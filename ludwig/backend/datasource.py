import contextlib
import logging
from typing import Any, Callable, Dict, Iterable, List, Optional, TYPE_CHECKING, Union

import urllib3
import ray
from packaging import version

from ludwig.utils.fs_utils import get_bytes_obj_from_http_path, is_http

from ray.data.block import Block
from ray.data.context import DatasetContext
from ray.data.datasource.binary_datasource import BinaryDatasource
from ray.data.datasource.datasource import ReadTask
from ray.data.datasource.file_based_datasource import (
    _resolve_paths_and_filesystem,
    _S3FileSystemWrapper,
    _wrap_s3_serialization_workaround,
)
from ray.data.impl.output_buffer import BlockOutputBuffer
from ray.data.impl.util import _check_pyarrow_version

_ray113 = version.parse("1.13") <= version.parse(ray.__version__) == version.parse("1.13.0")

if _ray113:
    # Code refactored in Ray 1.13
    from ray.data.datasource.file_meta_provider import BaseFileMetadataProvider, DefaultFileMetadataProvider
else:
    from ray.data.datasource.file_based_datasource import BaseFileMetadataProvider, DefaultFileMetadataProvider

if TYPE_CHECKING:
    import pyarrow

    if _ray113:
        # Only implemented starting in Ray 1.13
        from ray.data.datasource.partitioning import PathPartitionFilter

logger = logging.getLogger(__name__)


class BinaryIgnoreNoneTypeDatasource(BinaryDatasource):
    """Binary datasource, for reading and writing binary files. Ignores None values.

    Examples:
        >>> import ray
        >>> from ray.data.datasource import BinaryDatasource
        >>> source = BinaryDatasource() # doctest: +SKIP
        >>> ray.data.read_datasource( # doctest: +SKIP
        ...     source, paths=["/path/to/dir", None]).take()
        [b"file_data", ...]
    """

    def prepare_read(
        self,
        parallelism: int,
        paths: Union[str, List[str]],
        filesystem: Optional["pyarrow.fs.FileSystem"] = None,
        schema: Optional[Union[type, "pyarrow.lib.Schema"]] = None,
        open_stream_args: Optional[Dict[str, Any]] = None,
        meta_provider: BaseFileMetadataProvider = DefaultFileMetadataProvider(),
        partition_filter: "PathPartitionFilter" = None,
        # TODO(ekl) deprecate this once read fusion is available.
        _block_udf: Optional[Callable[[Block], Block]] = None,
        **reader_args,
    ) -> List[ReadTask]:
        """Creates and returns read tasks for a file-based datasource."""
        _check_pyarrow_version()
        import numpy as np

        read_stream = self._read_stream

        filesystem = _wrap_s3_serialization_workaround(filesystem)

        if open_stream_args is None:
            open_stream_args = {}

        def read_files(
            read_paths: List[str],
            fs: Union["pyarrow.fs.FileSystem", _S3FileSystemWrapper],
        ) -> Iterable[Block]:
            logger.debug(f"Reading {len(read_paths)} files.")
            if isinstance(fs, _S3FileSystemWrapper):
                fs = fs.unwrap()
            ctx = DatasetContext.get_current()
            output_buffer = BlockOutputBuffer(block_udf=_block_udf, target_max_block_size=ctx.target_max_block_size)
            for read_path in read_paths:
                # Get reader_args and open_stream_args only if valid path.
                if read_path is not None:
                    compression = open_stream_args.pop("compression", None)
                    if compression is None:
                        import pyarrow as pa

                        try:
                            # If no compression manually given, try to detect
                            # compression codec from path.
                            compression = pa.Codec.detect(read_path).name
                        except (ValueError, TypeError):
                            # Arrow's compression inference on the file path
                            # doesn't work for Snappy, so we double-check ourselves.
                            import pathlib

                            suffix = pathlib.Path(read_path).suffix
                            if suffix and suffix[1:] == "snappy":
                                compression = "snappy"
                            else:
                                compression = None
                    if compression == "snappy":
                        # Pass Snappy compression as a reader arg, so datasource subclasses
                        # can manually handle streaming decompression in
                        # self._read_stream().
                        reader_args["compression"] = compression
                        reader_args["filesystem"] = fs
                    elif compression is not None:
                        # Non-Snappy compression, pass as open_input_stream() arg so Arrow
                        # can take care of streaming decompression for us.
                        open_stream_args["compression"] = compression

                with self._open_input_source(fs, read_path, **open_stream_args) as f:
                    for data in read_stream(f, read_path, **reader_args):
                        output_buffer.add_block(data)
                        if output_buffer.has_next():
                            yield output_buffer.next()
            output_buffer.finalize()
            if output_buffer.has_next():
                yield output_buffer.next()

        # fix https://github.com/ray-project/ray/issues/24296
        parallelism = min(parallelism, len(paths))

        read_tasks = []
        for raw_paths in np.array_split(paths, parallelism):
            # Paths must be resolved and expanded
            read_paths = []
            file_sizes = []
            for raw_path in raw_paths:
                if raw_path is None or is_http(raw_path):
                    read_paths.append(raw_path)
                    file_sizes.append(None)  # unknown file size is None
                else:
                    resolved_path, filesystem = _resolve_paths_and_filesystem([raw_path], filesystem)
                    read_path, file_size = meta_provider.expand_paths(resolved_path, filesystem)
                    if partition_filter is not None:
                        read_path = partition_filter(read_path)
                    read_paths.append(read_path[0])
                    file_sizes.append(file_size[0])

            if len(read_paths) <= 0:
                continue

            meta = meta_provider(
                read_paths,
                schema,
                rows_per_file=self._rows_per_file(),
                file_sizes=file_sizes,
            )
            read_task = ReadTask(lambda read_paths=read_paths: read_files(read_paths, filesystem), meta)
            read_tasks.append(read_task)

        return read_tasks

    def _open_input_source(
        self,
        filesystem: "pyarrow.fs.FileSystem",
        path: str,
        **open_args,
    ) -> "pyarrow.NativeFile":
        """Opens a source path for reading and returns the associated Arrow NativeFile.

        The default implementation opens the source path as a sequential input stream.

        Implementations that do not support streaming reads (e.g. that require random
        access) should override this method.
        """
        if path is None or is_http(path):
            return contextlib.nullcontext()
        return filesystem.open_input_stream(path, **open_args)

    def _read_file(self, f: Union["pyarrow.NativeFile", contextlib.nullcontext], path: str, **reader_args):
        include_paths = reader_args.get("include_paths", False)
        if path is None:
            if include_paths:
                return [(path, None)]
            return [None]
        if is_http(path):
            try:
                data = get_bytes_obj_from_http_path(path)
            except urllib3.exceptions.HTTPError as e:
                logging.warning(e)
                data = None

            if include_paths:
                return [(path, data)]
            return [data]
        return super()._read_file(f, path, **reader_args)
