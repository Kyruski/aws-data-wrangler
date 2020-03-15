"""Amazon S3 Module."""

import concurrent.futures
import logging
import time
import uuid
from itertools import repeat
from typing import Any, Dict, List, Optional, Tuple, Union

import boto3  # type: ignore
import botocore.exceptions  # type: ignore
import pandas as pd  # type: ignore
import pyarrow  # type: ignore
import pyarrow.parquet  # type: ignore
import s3fs  # type: ignore

from awswrangler import _utils, exceptions

_COMPRESSION_2_EXT: Dict[Optional[str], str] = {None: "", "gzip": ".gz", "snappy": ".snappy"}

logger: logging.Logger = logging.getLogger(__name__)


def get_bucket_region(bucket: str, boto3_session: Optional[boto3.Session] = None) -> str:
    """Get bucket region name.

    Parameters
    ----------
    bucket : str
        Bucket name.
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 session will be used if boto3_session receive None.

    Returns
    -------
    str
        Region code (e.g. "us-east-1").

    Examples
    --------
    Using the default boto3 session

    >>> import awswrangler as wr
    >>> region = wr.s3.get_bucket_region("bucket-name")

    Using a custom boto3 session

    >>> import boto3
    >>> import awswrangler as wr
    >>> region = wr.s3.get_bucket_region("bucket-name", boto3_session=boto3.Session())

    """
    client_s3: boto3.client = _utils.client(service_name="s3", session=boto3_session)
    logger.debug(f"bucket: {bucket}")
    region: str = client_s3.get_bucket_location(Bucket=bucket)["LocationConstraint"]
    region = "us-east-1" if region is None else region
    logger.debug(f"region: {region}")
    return region


def does_object_exists(path: str, boto3_session: Optional[boto3.Session] = None) -> bool:
    """Check if object exists on S3.

    Parameters
    ----------
    path: str
        S3 path (e.g. s3://bucket/key).
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 session will be used if boto3_session receive None.

    Returns
    -------
    bool
        True if exists, False otherwise.

    Examples
    --------
    Using the default boto3 session

    >>> import awswrangler as wr
    >>> wr.s3.does_object_exists("s3://bucket/key_real")
    True
    >>> wr.s3.does_object_exists("s3://bucket/key_unreal")
    False

    Using a custom boto3 session

    >>> import boto3
    >>> import awswrangler as wr
    >>> wr.s3.does_object_exists("s3://bucket/key_real", boto3_session=boto3.Session())
    True
    >>> wr.s3.does_object_exists("s3://bucket/key_unreal", boto3_session=boto3.Session())
    False

    """
    client_s3: boto3.client = _utils.client(service_name="s3", session=boto3_session)
    bucket: str
    key: str
    bucket, key = path.replace("s3://", "").split("/", 1)
    try:
        client_s3.head_object(Bucket=bucket, Key=key)
        return True
    except botocore.exceptions.ClientError as ex:
        if ex.response["ResponseMetadata"]["HTTPStatusCode"] == 404:
            return False
        raise ex  # pragma: no cover


def list_objects(path: str, boto3_session: Optional[boto3.Session] = None) -> List[str]:
    """List Amazon S3 objects from a prefix.

    Parameters
    ----------
    path : str
        S3 path (e.g. s3://bucket/prefix).
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 session will be used if boto3_session receive None.

    Returns
    -------
    List[str]
        List of objects paths.

    Examples
    --------
    Using the default boto3 session

    >>> import awswrangler as wr
    >>> wr.s3.list_objects("s3://bucket/prefix")
    ["s3://bucket/prefix0", "s3://bucket/prefix1", "s3://bucket/prefix2"]

    Using a custom boto3 session

    >>> import boto3
    >>> import awswrangler as wr
    >>> wr.s3.list_objects("s3://bucket/prefix", boto3_session=boto3.Session())
    ["s3://bucket/prefix0", "s3://bucket/prefix1", "s3://bucket/prefix2"]

    """
    client_s3: boto3.client = _utils.client(service_name="s3", session=boto3_session)
    paginator = client_s3.get_paginator("list_objects_v2")
    bucket: str
    prefix: str
    bucket, prefix = _utils.parse_path(path=path)
    response_iterator = paginator.paginate(Bucket=bucket, Prefix=prefix, PaginationConfig={"PageSize": 1000})
    paths: List[str] = []
    for page in response_iterator:
        contents: Optional[List] = page.get("Contents")
        if contents is not None:
            for content in contents:
                if (content is not None) and ("Key" in content):
                    key: str = content["Key"]
                    paths.append(f"s3://{bucket}/{key}")
    return paths


def _path2list(path: Union[str, List[str]], boto3_session: Optional[boto3.Session]) -> List[str]:
    if isinstance(path, str):  # prefix
        paths: List[str] = list_objects(path=path, boto3_session=boto3_session)
    elif isinstance(path, list):
        paths = path
    else:
        raise exceptions.InvalidArgumentType(f"{type(path)} is not a valid path type. Please, use str or List[str].")
    return paths


def delete_objects(
    path: Union[str, List[str]], use_threads: bool = True, boto3_session: Optional[boto3.Session] = None
) -> None:
    """Delete Amazon S3 objects from a received S3 prefix or list of S3 objects paths.

    Note
    ----
    In case of ``use_threads=True`` the number of process that will be spawned will be get from os.cpu_count().

    Parameters
    ----------
    path : Union[str, List[str]]
        S3 prefix (e.g. s3://bucket/prefix) or list of S3 objects paths (e.g. [s3://bucket/key0, s3://bucket/key1]).
    use_threads : bool
        True to enable concurrent requests, False to disable multiple threads.
        If enabled os.cpu_count() will be used as the max number of threads.
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 session will be used if boto3_session receive None.

    Returns
    -------
    None
        None.

    Examples
    --------
    >>> import awswrangler as wr
    >>> wr.s3.delete_objects(["s3://bucket/key0", "s3://bucket/key1"])  # Delete both objects
    >>> wr.s3.delete_objects("s3://bucket/prefix")  # Delete all objects under the received prefix

    """
    paths: List[str] = _path2list(path=path, boto3_session=boto3_session)
    if len(paths) < 1:
        return
    client_s3: boto3.client = _utils.client(service_name="s3", session=boto3_session)
    buckets: Dict[str, List[str]] = _split_paths_by_bucket(paths=paths)
    for bucket, keys in buckets.items():
        chunks: List[List[str]] = _utils.chunkify(lst=keys, max_length=1_000)
        if use_threads is False:
            for chunk in chunks:
                _delete_objects(bucket=bucket, keys=chunk, client_s3=client_s3)
        else:
            cpus: int = _utils.ensure_cpu_count(use_threads=use_threads)
            with concurrent.futures.ThreadPoolExecutor(max_workers=cpus) as executor:
                executor.map(_delete_objects, repeat(bucket), chunks, repeat(client_s3))


def _split_paths_by_bucket(paths: List[str]) -> Dict[str, List[str]]:
    buckets: Dict[str, List[str]] = {}
    bucket: str
    key: str
    for path in paths:
        bucket, key = _utils.parse_path(path=path)
        if bucket not in buckets:
            buckets[bucket] = []
        buckets[bucket].append(key)
    return buckets


def _delete_objects(bucket: str, keys: List[str], client_s3: boto3.client) -> None:
    logger.debug(f"len(keys): {len(keys)}")
    batch: List[Dict[str, str]] = [{"Key": key} for key in keys]
    client_s3.delete_objects(Bucket=bucket, Delete={"Objects": batch})


def describe_objects(
    path: Union[str, List[str]],
    wait_time: Optional[Union[int, float]] = None,
    use_threads: bool = True,
    boto3_session: Optional[boto3.Session] = None,
) -> Dict[str, Dict[str, Any]]:
    """Describe Amazon S3 objects from a received S3 prefix or list of S3 objects paths.

    Fetch attributes like ContentLength, DeleteMarker, LastModified, ContentType, etc
    The full list of attributes can be explored under the boto3 head_object documentation:
    https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html#S3.Client.head_object

    Note
    ----
    In case of ``use_threads=True`` the number of process that will be spawned will be get from os.cpu_count().

    Parameters
    ----------
    path : Union[str, List[str]]
        S3 prefix (e.g. s3://bucket/prefix) or list of S3 objects paths (e.g. [s3://bucket/key0, s3://bucket/key1]).
    wait_time : Union[int,float], optional
        How much time (seconds) should Wrangler try to reach this objects.
        Very useful to overcome eventual consistence issues.
        `None` means only a single try will be done.
    use_threads : bool
        True to enable concurrent requests, False to disable multiple threads.
        If enabled os.cpu_count() will be used as the max number of threads.
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 session will be used if boto3_session receive None.

    Returns
    -------
    Dict[str, Dict[str, Any]]
        Return a dictionary of objects returned from head_objects where the key is the object path.
        The response object can be explored here:
        https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html#S3.Client.head_object

    Examples
    --------
    >>> import awswrangler as wr
    >>> descs0 = wr.s3.describe_objects(["s3://bucket/key0", "s3://bucket/key1"])  # Describe both objects
    >>> descs1 = wr.s3.describe_objects("s3://bucket/prefix")  # Describe all objects under the prefix
    >>> descs2 = wr.s3.describe_objects("s3://bucket/prefix", wait_time=30)  # Overcoming eventual consistence issues

    """
    paths: List[str] = _path2list(path=path, boto3_session=boto3_session)
    if len(paths) < 1:
        return {}
    client_s3: boto3.client = _utils.client(service_name="s3", session=boto3_session)
    resp_list: List[Tuple[str, Dict[str, Any]]]
    if use_threads is False:
        resp_list = [_describe_object(path=p, wait_time=wait_time, client_s3=client_s3) for p in paths]
    else:
        cpus: int = _utils.ensure_cpu_count(use_threads=use_threads)
        with concurrent.futures.ThreadPoolExecutor(max_workers=cpus) as executor:
            resp_list = list(executor.map(_describe_object, paths, repeat(wait_time), repeat(client_s3)))
    desc_list: Dict[str, Dict[str, Any]] = dict(resp_list)
    return desc_list


def _describe_object(
    path: str, wait_time: Optional[Union[int, float]], client_s3: boto3.client
) -> Tuple[str, Dict[str, Any]]:
    wait_time = int(wait_time) if isinstance(wait_time, float) else wait_time
    tries: int = wait_time if (wait_time is not None) and (wait_time > 0) else 1
    bucket: str
    key: str
    bucket, key = _utils.parse_path(path=path)
    desc: Dict[str, Any] = {}
    for i in range(tries, 0, -1):
        try:
            desc = client_s3.head_object(Bucket=bucket, Key=key)
            break
        except botocore.exceptions.ClientError as e:  # pragma: no cover
            if e.response["ResponseMetadata"]["HTTPStatusCode"] == 404:  # Not Found
                logger.debug(f"Object not found. {i} seconds remaining to wait.")
                if i == 1:  # Last try, there is no more need to sleep
                    break
                time.sleep(1)
            else:
                raise e
    return path, desc


def size_objects(
    path: Union[str, List[str]],
    wait_time: Optional[Union[int, float]] = None,
    use_threads: bool = True,
    boto3_session: Optional[boto3.Session] = None,
) -> Dict[str, Optional[int]]:
    """Get the size (ContentLength) in bytes of Amazon S3 objects from a received S3 prefix or list of S3 objects paths.

    Note
    ----
    In case of ``use_threads=True`` the number of process that will be spawned will be get from os.cpu_count().

    Parameters
    ----------
    path : Union[str, List[str]]
        S3 prefix (e.g. s3://bucket/prefix) or list of S3 objects paths (e.g. [s3://bucket/key0, s3://bucket/key1]).
    wait_time : Union[int,float], optional
        How much time (seconds) should Wrangler try to reach this objects.
        Very useful to overcome eventual consistence issues.
        `None` means only a single try will be done.
    use_threads : bool
        True to enable concurrent requests, False to disable multiple threads.
        If enabled os.cpu_count() will be used as the max number of threads.
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 session will be used if boto3_session receive None.

    Returns
    -------
    Dict[str, Optional[int]]
        Dictionary where the key is the object path and the value is the object size.

    Examples
    --------
    >>> import awswrangler as wr
    >>> sizes0 = wr.s3.size_objects(["s3://bucket/key0", "s3://bucket/key1"])  # Get the sizes of both objects
    >>> sizes1 = wr.s3.size_objects("s3://bucket/prefix")  # Get the sizes of all objects under the received prefix
    >>> sizes2 = wr.s3.size_objects("s3://bucket/prefix", wait_time=30)  # Overcoming eventual consistence issues

    """
    desc_list: Dict[str, Dict[str, Any]] = describe_objects(
        path=path, wait_time=wait_time, use_threads=use_threads, boto3_session=boto3_session
    )
    size_list: Dict[str, Optional[int]] = {k: d.get("ContentLength", None) for k, d in desc_list.items()}
    return size_list


def to_csv(df: pd.DataFrame, path: str, boto3_session: Optional[boto3.Session] = None, **pd_kwargs) -> None:
    """Write CSV file on Amazon S3.

    Parameters
    ----------
    df: pandas.DataFrame
        Pandas DataFrame https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.DataFrame.html
    path : str
        Amazon S3 path (e.g. s3://bucket/filename.csv).
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 Session will be used if boto3_session receive None.
    pd_kwargs:
        keyword arguments forwarded to pandas.DataFrame.to_csv()
        https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.DataFrame.to_csv.html

    Returns
    -------
    None
        None.

    Examples
    --------
    Writing single file with filename

    >>> import awswrangler as wr
    >>> import pandas as pd
    >>> wr.s3.to_csv(
    ...     df=pd.DataFrame({"col": [1, 2, 3]}),
    ...     path="s3://bucket/filename.csv",
    ... )

    """
    fs: s3fs.S3FileSystem = _utils.get_fs(session=boto3_session)
    with fs.open(path, "w") as f:
        df.to_csv(path_or_buf=f, **pd_kwargs)


def to_parquet(
    df: pd.DataFrame,
    path: str,
    index: bool = False,
    compression: Optional[str] = "snappy",
    use_threads: bool = True,
    boto3_session: Optional[boto3.Session] = None,
    dataset: bool = False,
    partition_cols: Optional[List[str]] = None,
    mode: Optional[str] = None,
) -> List[str]:
    """Write Parquet file or dataset on Amazon S3.

    The concept of Dataset goes beyond the simple idea of files and enable more
    complex features like partitioning and catalog integration (AWS Glue Catalog).

    Note
    ----
    In case of ``use_threads=True`` the number of process that will be spawned will be get from os.cpu_count().

    Parameters
    ----------
    df: pandas.DataFrame
        Pandas DataFrame https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.DataFrame.html
    path : str
        S3 path (for file e.g. ``s3://bucket/prefix/filename.parquet``) (for dataset e.g. ``s3://bucket/prefix``).
    index : bool
        True to store the DataFrame index in file, otherwise False to ignore it.
    compression: str, optional
        Compression style (``None``, ``snappy``, ``gzip``).
    use_threads : bool
        True to enable concurrent requests, False to disable multiple threads.
        If enabled os.cpu_count() will be used as the max number of threads.
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 session will be used if boto3_session receive None.
    dataset: bool
        If True store a parquet dataset instead of a single file.
        If True, enable all follow arguments (partition_cols, mode).
    partition_cols: List[str], optional
        List of column names that will be used to create partitions. Only takes effect if dataset=True.
    mode: str, optional
        ``append`` (Default), ``overwrite``, ``partition_upsert``. Only takes effect if dataset=True.

    Returns
    -------
    List[str]
        List with the s3 paths created.

    Examples
    --------
    Writing single file

    >>> import awswrangler as wr
    >>> import pandas as pd
    >>> wr.s3.to_parquet(
    ...     df=pd.DataFrame({"col": [1, 2, 3]}),
    ...     path="s3://bucket/prefix/my_file.parquet",
    ... )
    ["s3://bucket/prefix/my_file.parquet"]

    Writing partitioned dataset

    >>> import awswrangler as wr
    >>> import pandas as pd
    >>> wr.s3.to_parquet(
    ...     df=pd.DataFrame({
    ...         "col": [1, 2, 3],
    ...         "col2": ["A", "A", "B"]
    ...     }),
    ...     path="s3://bucket/prefix",
    ...     dataset=True,
    ...     partition_cols=["col2"]
    ... )
    ["s3://bucket/prefix/col2=A/xxx.snappy.parquet", "s3://bucket/prefix/col2=B/xxx.snappy.parquet"]

    """
    cpus: int = _utils.ensure_cpu_count(use_threads=use_threads)
    fs: s3fs.S3FileSystem = _utils.get_fs(session=boto3_session)
    compression_ext: Optional[str] = _COMPRESSION_2_EXT.get(compression, None)
    if compression_ext is None:
        raise exceptions.InvalidCompression(f"{compression} is invalid, please use None, snappy or gzip.")
    if dataset is False:
        if partition_cols:
            raise exceptions.InvalidArgumentCombination("Please, pass dataset=True to be able to use partition_cols.")
        if mode:
            raise exceptions.InvalidArgumentCombination("Please pass dataset=True to be able to use mode.")
        paths = [_to_parquet_file(df=df, path=path, index=index, compression=compression, cpus=cpus, fs=fs)]
    else:
        mode = "append" if mode is None else mode
        paths = _to_parquet_dataset(
            df=df,
            path=path,
            index=index,
            compression=compression,
            compression_ext=compression_ext,
            cpus=cpus,
            fs=fs,
            use_threads=use_threads,
            partition_cols=partition_cols,
            mode=mode,
            boto3_session=boto3_session,
        )
    return paths


def _to_parquet_dataset(
    df: pd.DataFrame,
    path: str,
    index: bool,
    compression: Optional[str],
    compression_ext: str,
    cpus: int,
    fs: s3fs.S3FileSystem,
    use_threads: bool,
    mode: str,
    partition_cols: Optional[List[str]] = None,
    boto3_session: Optional[boto3.Session] = None,
) -> List[str]:
    paths: List[str] = []
    path = path if path[-1] == "/" else f"{path}/"
    if mode not in ["append", "overwrite", "partitions_upsert"]:
        raise exceptions.InvalidArgumentValue(
            f"{mode} is a invalid mode, please use append, overwrite or partitions_upsert."
        )
    if (mode == "overwrite") or ((mode == "partitions_upsert") and (not partition_cols)):
        delete_objects(path=path, use_threads=use_threads, boto3_session=boto3_session)
    if not partition_cols:
        file_path: str = f"{path}{uuid.uuid4().hex}{compression_ext}.parquet"
        _to_parquet_file(df=df, path=file_path, index=index, compression=compression, cpus=cpus, fs=fs)
        paths.append(file_path)
    else:
        for keys, subgroup in df.groupby(by=partition_cols, observed=True):
            subgroup = subgroup.drop(partition_cols, axis="columns")
            keys = (keys,) if not isinstance(keys, tuple) else keys
            subdir = "/".join([f"{name}={val}" for name, val in zip(partition_cols, keys)])
            prefix: str = f"{path}{subdir}/"
            if mode == "partitions_upsert":
                delete_objects(path=prefix, use_threads=use_threads)
            file_path = f"{prefix}{uuid.uuid4().hex}{compression_ext}.parquet"
            _to_parquet_file(df=subgroup, path=file_path, index=index, compression=compression, cpus=cpus, fs=fs)
            paths.append(file_path)
    return paths


def _to_parquet_file(
    df: pd.DataFrame, path: str, index: bool, compression: Optional[str], cpus: int, fs: s3fs.S3FileSystem
) -> str:
    table: pyarrow.Table = pyarrow.Table.from_pandas(df=df, nthreads=cpus, preserve_index=index, safe=False)
    pyarrow.parquet.write_table(
        table=table,
        where=path,
        write_statistics=True,
        use_dictionary=True,
        filesystem=fs,
        coerce_timestamps="ms",
        compression=compression,
        flavor="spark",
    )
    return path


def read_csv(
    path: Union[str, List[str]],
    use_threads: bool = True,
    boto3_session: Optional[boto3.Session] = None,
    **pandas_kwargs,
) -> pd.DataFrame:
    """Read CSV file(s) from from a received S3 prefix or list of S3 objects paths.

    Note
    ----
    In case of ``use_threads=True`` the number of process that will be spawned will be get from os.cpu_count().

    Parameters
    ----------
    path : Union[str, List[str]]
        S3 prefix (e.g. s3://bucket/prefix) or list of S3 objects paths (e.g. ``[s3://bucket/key0, s3://bucket/key1]``).
    use_threads : bool
        True to enable concurrent requests, False to disable multiple threads.
        If enabled os.cpu_count() will be used as the max number of threads.
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 session will be used if boto3_session receive None.
    pandas_kwargs:
        keyword arguments forwarded to pandas.read_csv().
        https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_csv.html

    Returns
    -------
    pandas.DataFrame
        Pandas DataFrame.

    Examples
    --------
    Reading all CSV files under a prefix

    >>> import awswrangler as wr
    >>> df = wr.s3.read_csv(path="s3://bucket/prefix/")

    Reading all CSV files from a list

    >>> import awswrangler as wr
    >>> df = wr.s3.read_csv(path=["s3://bucket/filename0.csv", "s3://bucket/filename1.csv"])

    """
    paths: List[str] = _path2list(path=path, boto3_session=boto3_session)
    if use_threads is False:
        df: pd.DataFrame = pd.concat(
            objs=[_read_csv(path=p, boto3_session=boto3_session, pandas_args=pandas_kwargs) for p in paths],
            ignore_index=True,
            sort=False,
        )
    else:
        cpus: int = _utils.ensure_cpu_count(use_threads=use_threads)
        with concurrent.futures.ThreadPoolExecutor(max_workers=cpus) as executor:
            df = pd.concat(
                objs=executor.map(_read_csv, paths, repeat(boto3_session), repeat(pandas_kwargs)),
                ignore_index=True,
                sort=False,
            )
    return df


def _read_csv(path: str, boto3_session: boto3.Session, pandas_args) -> pd.DataFrame:
    fs: s3fs.S3FileSystem = _utils.get_fs(session=boto3_session)
    with fs.open(path, "r") as f:
        return pd.read_csv(filepath_or_buffer=f, **pandas_args)


def read_parquet(
    path: Union[str, List[str]],
    filters: Optional[Union[List[Tuple], List[List[Tuple]]]] = None,
    columns: Optional[List[str]] = None,
    dataset: bool = False,
    use_threads: bool = True,
    boto3_session: Optional[boto3.Session] = None,
) -> pd.DataFrame:
    """Read Apache Parquet file(s) from from a received S3 prefix or list of S3 objects paths.

    The concept of Dataset goes beyond the simple idea of files and enable more
    complex features like partitioning and catalog integration (AWS Glue Catalog).

    Note
    ----
    In case of ``use_threads=True`` the number of process that will be spawned will be get from os.cpu_count().

    Parameters
    ----------
    path : Union[str, List[str]]
        S3 prefix (e.g. s3://bucket/prefix) or list of S3 objects paths (e.g. [s3://bucket/key0, s3://bucket/key1]).
    filters: Union[List[Tuple], List[List[Tuple]]], optional
        List of filters to apply, like ``[[('x', '=', 0), ...], ...]``.
    columns: List[str], optional
        Names of columns to read from the file(s)
    dataset: bool
        If True read a parquet dataset instead of simple file(s) loading all the related partitions as columns.
    use_threads : bool
        True to enable concurrent requests, False to disable multiple threads.
        If enabled os.cpu_count() will be used as the max number of threads.
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 session will be used if boto3_session receive None.

    Returns
    -------
    pandas.DataFrame
        Pandas DataFrame.

    Examples
    --------
    Reading all Parquet files under a prefix

    >>> import awswrangler as wr
    >>> df = wr.s3.read_parquet(path="s3://bucket/prefix/")

    Reading all Parquet files from a list

    >>> import awswrangler as wr
    >>> df = wr.s3.read_parquet(path=["s3://bucket/filename0.parquet", "s3://bucket/filename1.parquet"])

    """
    if dataset is False:
        path_or_paths: Union[str, List[str]] = _path2list(path=path, boto3_session=boto3_session)
    else:
        path_or_paths = path
    fs: s3fs.S3FileSystem = _utils.get_fs(session=boto3_session)
    cpus: int = _utils.ensure_cpu_count(use_threads=use_threads)
    data: pyarrow.parquet.ParquetDataset = pyarrow.parquet.ParquetDataset(
        path_or_paths=path_or_paths, filesystem=fs, metadata_nthreads=cpus, filters=filters
    )
    return data.read(columns=columns, use_threads=use_threads).to_pandas(
        use_threads=use_threads, split_blocks=True, self_destruct=True, integer_object_nulls=False
    )
