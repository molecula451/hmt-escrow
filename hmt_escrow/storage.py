import hashlib
import json
import logging
import os
from typing import Dict, Tuple, Optional, Union

import boto3
from botocore.exceptions import ClientError

from hmt_escrow import crypto

SHARED_MAC_DATA: bytes = os.getenv(
    "SHARED_MAC", "9da0d3721774843193737244a0f3355191f66ff7321e83eae83f7f746eb34350"
).encode("ascii")

logging.getLogger("boto").setLevel(logging.INFO)
logging.getLogger("botocore").setLevel(logging.INFO)
logging.getLogger("boto3").setLevel(logging.INFO)

DEBUG = "true" in os.getenv("DEBUG", "false").lower()
LOG = logging.getLogger("hmt_escrow.storage")
LOG.setLevel(logging.DEBUG if DEBUG else logging.INFO)

ESCROW_BUCKETNAME = os.getenv("ESCROW_BUCKETNAME", "escrow-results")
ESCROW_PUBLIC_BUCKETNAME = os.getenv(
    "ESCROW_PUBLIC_BUCKETNAME", "escrow-public-results"
)

ESCROW_AWS_ACCESS_KEY_ID = os.getenv("ESCROW_AWS_ACCESS_KEY_ID", "minio")
ESCROW_AWS_SECRET_ACCESS_KEY = os.getenv("ESCROW_AWS_SECRET_ACCESS_KEY", "minio123")

ESCROW_AWS_REGION = os.getenv("ESCROW_AWS_REGION", "us-west-2")

ESCROW_ENDPOINT_URL = os.getenv("ESCROW_ENDPOINT_URL", "http://minio:9000")
ESCROW_PUBLIC_RESULTS_URL = os.getenv("ESCROW_PUBLIC_RESULTS_URL", ESCROW_ENDPOINT_URL)


class StorageClientError(Exception):
    """Raises when some error happens when interacting with storage."""

    pass


class StorageFileNotFoundError(StorageClientError):
    """Raises when some error happens when file is not found by its key."""

    pass


def _connect_s3():
    try:
        return boto3.client(
            "s3",
            aws_access_key_id=ESCROW_AWS_ACCESS_KEY_ID,
            aws_secret_access_key=ESCROW_AWS_SECRET_ACCESS_KEY,
            endpoint_url=ESCROW_ENDPOINT_URL,
            region_name=ESCROW_AWS_REGION,
        )
    except Exception as e:
        LOG.error(f"Connection with S3 failed because of: {e}")
        raise e


def get_bucket(public: bool = False) -> str:
    """Retrieves correct bucket according to ACL visibility.

    Args:
          public(bool): whether the public bucket should be retrieved or internal one.

    Returns:
        str: bucket name
    """
    return ESCROW_PUBLIC_BUCKETNAME if public is True else ESCROW_BUCKETNAME


def get_public_bucket_url(key: str) -> str:
    """Retrieves public bucket URL

    Args:
        key(str): File key uploaded in storage

    Returns:
        str: Bucket public URL
    """
    base_url = (
        ESCROW_PUBLIC_RESULTS_URL[:-1]
        if ESCROW_PUBLIC_RESULTS_URL.endswith("/")
        else ESCROW_PUBLIC_RESULTS_URL
    )
    return f"{base_url}/{key}"


def get_key_from_url(url: str) -> str:
    """Retrieve key from storage URL.

    Args:
        url(str): File URL to point where it is stored

    Returns:
        str: file key in storage.
    """
    if url.startswith("http"):
        # URL is fully qualified URL. Let's split it and try to retrieve key from last part of it.
        key = url.split("/")[-1]
        assert key.startswith("s3")
        return key

    # If not fully qualified http URL, the key is the URL
    return url


def download_from_storage(key: str, public: bool = False) -> bytes:
    """Downloads data from storage if exists.

    Args:
         key(str): file key to find it in storage to be downloaded.
         public(bool): whether file is public
    """
    LOG.debug("Downloading s3 key: {}".format(key))
    bucket_name = get_bucket(public=public)

    BOTO3_CLIENT = _connect_s3()
    try:
        response = BOTO3_CLIENT.get_object(Bucket=bucket_name, Key=key)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            raise StorageFileNotFoundError("No object found - returning empty")

        raise StorageClientError(str(e))

    except Exception as e:
        LOG.warning(
            f"Reading the key {key} with S3 failed (public: {public}"
            f" because of: {str(e)}"
        )
        raise e
    else:
        return response["Body"].read()


def download(key: str, private_key: bytes, public: bool = False) -> Dict:
    """Download a key, decrypt it, and output it as a binary string.

    Args:
        key (str): This is the hash code returned when uploading.
        private_key (str): The private_key to decrypt this string with.
        public(bool): whether file is public

    Returns:
        Dict: returns the contents of the filename which was previously uploaded.

    Raises:
        Exception: if reading from fails.

    """
    try:
        content = download_from_storage(key=key, public=public)
        artifact = (
            crypto.decrypt(private_key, content)
            if crypto.is_encrypted(content) is True
            else content.decode()
        )
    except Exception as e:
        LOG.warning(
            "Reading the key {!r} with private key {!r} with S3 failed"
            " because of: {!r}".format(key, private_key, e)
        )
        raise e
    return json.loads(artifact)


def upload(
    msg: Dict,
    public_key: bytes,
    encrypt_data: Optional[bool] = True,
    use_public_bucket: Optional[bool] = False,
) -> Tuple[str, str]:
    """Upload and encrypt a string for later retrieval.
    This can be manifest files, results, or anything that's been already
    encrypted.

    Args:
        msg (Dict): The message to upload and encrypt.
        public_key (bytes): The public_key to encrypt the file for.
        encrypt_data (bool): Whether data must be encrypted before uploading.
        use_public_bucket (bool): Whether data must be stored in the public bucket.

    Returns:
        Tuple[str, str]: returns the contents of the filename which was previously uploaded.

    Raises:
        Exception: if adding bytes fails.

    """
    try:
        artifact = json.dumps(msg, sort_keys=True)
    except Exception as e:
        LOG.error("Can't extract the json from the dict")
        raise e

    content = artifact.encode("utf-8")

    hash_ = hashlib.sha1(content).hexdigest()
    key = f"s3{hash_}"

    # If encryption is on, even if usage of public bucket is true, encrypted data is always private
    is_public = use_public_bucket is True and encrypt_data is False
    bucket_name = get_bucket(public=is_public)

    bucket_kwargs: Dict[str, Union[str, bytes]] = {"Key": key, "Bucket": bucket_name}

    if encrypt_data is True:
        # If encryption is enabled, the bucket is private and data is encrypted
        bucket_kwargs.update({"Body": crypto.encrypt(public_key, artifact)})
    else:
        # If encryption is off, file will be publicly readable
        bucket_kwargs.update({"ACL": "public-read", "Body": content})

    BOTO3_CLIENT = _connect_s3()
    BOTO3_CLIENT.put_object(**bucket_kwargs)

    LOG.debug(f"Uploaded to S3, key: {key}")
    return hash_, key
