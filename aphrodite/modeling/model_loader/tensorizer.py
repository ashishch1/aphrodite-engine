import argparse
import dataclasses
import io
import os
import time
import typing
from dataclasses import dataclass
from functools import partial
from typing import Generator, Optional, Tuple, Type, Union

import torch
from loguru import logger
from torch import nn
from transformers import PretrainedConfig

from aphrodite.common.config import ModelConfig, ParallelConfig
from aphrodite.engine.aphrodite_engine import AphroditeEngine
from aphrodite.modeling.layers.vocab_parallel_embedding import \
    VocabParallelEmbedding
from aphrodite.quantization.base_config import QuantizationConfig

tensorizer_error_msg = None

try:
    from tensorizer import (DecryptionParams, EncryptionParams,
                            TensorDeserializer, TensorSerializer)
    from tensorizer.stream_io import open_stream
    from tensorizer.utils import (convert_bytes, get_mem_usage,
                                  no_init_or_tensor)

    _read_stream, _write_stream = (partial(
        open_stream,
        mode=mode,
    ) for mode in ("rb", "wb+"))
except ImportError as e:
    tensorizer_error_msg = e

__all__ = [
    'EncryptionParams', 'DecryptionParams', 'TensorDeserializer',
    'TensorSerializer', 'open_stream', 'convert_bytes', 'get_mem_usage',
    'no_init_or_tensor', 'TensorizerConfig'
]


@dataclass
class TensorizerConfig:
    tensorizer_uri: Union[io.BufferedIOBase, io.RawIOBase, typing.BinaryIO,
                          str, bytes, os.PathLike, int]
    aphrodite_tensorized: Optional[bool] = False
    verify_hash: Optional[bool] = False
    num_readers: Optional[int] = None
    encryption_keyfile: Optional[str] = None
    s3_access_key_id: Optional[str] = None
    s3_secret_access_key: Optional[str] = None
    s3_endpoint: Optional[str] = None
    model_class: Optional[Type[torch.nn.Module]] = None
    hf_config: Optional[PretrainedConfig] = None
    dtype: Optional[Union[str, torch.dtype]] = None

    def _construct_tensorizer_args(self) -> "TensorizerArgs":
        tensorizer_args = {
            "tensorizer_uri": self.tensorizer_uri,
            "aphrodite_tensorized": self.aphrodite_tensorized,
            "verify_hash": self.verify_hash,
            "num_readers": self.num_readers,
            "encryption_keyfile": self.encryption_keyfile,
            "s3_access_key_id": self.s3_access_key_id,
            "s3_secret_access_key": self.s3_secret_access_key,
            "s3_endpoint": self.s3_endpoint,
        }
        return TensorizerArgs(**tensorizer_args)

    def verify_with_parallel_config(
        self,
        parallel_config: "ParallelConfig",
    ) -> None:
        if (parallel_config.tensor_parallel_size > 1
                and self.tensorizer_uri is not None):
            raise ValueError(
                "Loading to multiple GPUs is not currently supported with "
                "aphrodite-serialized models. Please set "
                "tensor_parallel_size=1. or use a non-aphrodite-serialized "
                "model, such as a serialized Hugging Face `PretrainedModel`.")

    def verify_with_model_config(self, model_config: "ModelConfig") -> None:
        if (model_config.quantization is not None
                and self.tensorizer_uri is not None):
            logger.warning(
                "Loading a model using Tensorizer with quantization on "
                "aphrodite is unstable and may lead to errors.")


def load_with_tensorizer(tensorizer_config: TensorizerConfig,
                         **extra_kwargs) -> nn.Module:
    tensorizer = TensorizerAgent(tensorizer_config, **extra_kwargs)
    return tensorizer.deserialize()


@dataclass
class TensorizerArgs:
    tensorizer_uri: Union[io.BufferedIOBase, io.RawIOBase, typing.BinaryIO,
                          str, bytes, os.PathLike, int]
    aphrodite_tensorized: Optional[bool] = False
    verify_hash: Optional[bool] = False
    num_readers: Optional[int] = None
    encryption_keyfile: Optional[str] = None
    s3_access_key_id: Optional[str] = None
    s3_secret_access_key: Optional[str] = None
    s3_endpoint: Optional[str] = None
    """
  Args for the TensorizerAgent class. These are used to configure the behavior 
  of the TensorDeserializer when loading tensors from a serialized model.
  
  Args:
      tensorizer_uri: Path to serialized model tensors. Can be a local file 
          path or a S3 URI.
      aphrodite_tensorized: If True, indicates that the serialized model is a 
          aphrodite model. This is used to determine the behavior of the 
          TensorDeserializer when loading tensors from a serialized model.
          It is far faster to deserialize a aphrodite model as it utilizes
          ttensorizer's optimized GPU loading. Note that this is now
          deprecated, as serialized Aphrodite models are now automatically
          inferred as Aphrodite models.
      verify_hash: If True, the hashes of each tensor will be verified against 
          the hashes stored in the metadata. A `HashMismatchError` will be 
          raised if any of the hashes do not match.
      num_readers: Controls how many threads are allowed to read concurrently
          from the source file. Default is `None`, which will dynamically set
          the number of readers based on the number of available 
          resources and model size. This greatly increases performance.
      encryption_keyfile: File path to a binary file containing a  
          binary key to use for decryption. `None` (the default) means 
          no decryption. See the example script in 
          examples/tensorize_aphrodite_model.py. 
      s3_access_key_id: The access key for the S3 bucket. Can also be set via
          the S3_ACCESS_KEY_ID environment variable.
      s3_secret_access_key: The secret access key for the S3 bucket. Can also
          be set via the S3_SECRET_ACCESS_KEY environment variable.
      s3_endpoint: The endpoint for the S3 bucket. Can also be set via the
          S3_ENDPOINT_URL environment variable.
  """

    def __post_init__(self):
        self.file_obj = self.tensorizer_uri
        self.s3_access_key_id = (self.s3_access_key_id
                                 or os.environ.get("S3_ACCESS_KEY_ID")) or None
        self.s3_secret_access_key = (
            self.s3_secret_access_key
            or os.environ.get("S3_SECRET_ACCESS_KEY")) or None
        self.s3_endpoint = (self.s3_endpoint
                            or os.environ.get("S3_ENDPOINT_URL")) or None
        self.stream_params = {
            "s3_access_key_id": self.s3_access_key_id,
            "s3_secret_access_key": self.s3_secret_access_key,
            "s3_endpoint": self.s3_endpoint,
        }

        self.deserializer_params = {
            "verify_hash": self.verify_hash,
            "encryption": self.encryption_keyfile,
            "num_readers": self.num_readers
        }
        if self.encryption_keyfile:
            with open_stream(
                    self.encryption_keyfile,
                    **self.stream_params,
            ) as stream:
                key = stream.read()
                decryption_params = DecryptionParams.from_key(key)
                self.deserializer_params['encryption'] = decryption_params

    @staticmethod
    def add_cli_args(
            parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
        """Tensorizer CLI arguments"""

        # Tensorizer options arg group
        group = parser.add_argument_group(
            'tensorizer options',
            description=('Options for configuring the behavior of the'
                         ' tensorizer deserializer when '
                         'load_format=tensorizer is specified when '
                         'initializing an AphroditeEngine, either via the CLI '
                         'when running the Aphrodite OpenAI inference server '
                         'with a JSON string passed to '
                         '--model-loader-extra-config or as arguments given '
                         'to TensorizerConfig when passed to '
                         'model_loader_extra_config in the constructor '
                         'for AphroditeEngine.'))

        group.add_argument(
            "--tensorizer-uri",
            help="Path to serialized model tensors. Can be a local file path,"
            " or an HTTP(S) or S3 URI.",
        )
        group.add_argument(
            "--verify-hash",
            action="store_true",
            help="If enabled, the hashes of each tensor will be verified"
            " against the hashes stored in the file metadata. An exception"
            " will be raised if any of the hashes do not match.",
        )
        group.add_argument(
            "--encryption-keyfile",
            default=None,
            help="The file path to a binary file containing a binary key to "
            "use for decryption. Can be a file path or S3 network URI.")
        group.add_argument(
            "--num-readers",
            default=None,
            type=int,
            help="Controls how many threads are allowed to read concurrently "
            "from the source file. Default is `None`, which will dynamically "
            "set the number of readers based on the available resources "
            "and model size. This greatly increases performance.")
        group.add_argument(
            "--s3-access-key-id",
            default=None,
            help="The access key for the S3 bucket. Can also be set via the "
            "S3_ACCESS_KEY_ID environment variable.",
        )
        group.add_argument(
            "--s3-secret-access-key",
            default=None,
            help="The secret access key for the S3 bucket. Can also be set via "
            "the S3_SECRET_ACCESS_KEY environment variable.",
        )
        group.add_argument(
            "--s3-endpoint",
            default=None,
            help="The endpoint for the S3 bucket. Can also be set via the "
            "S3_ENDPOINT_URL environment variable.",
        )

        return parser

    @classmethod
    def from_cli_args(cls, args: argparse.Namespace) -> "TensorizerArgs":
        attrs = [attr.name for attr in dataclasses.fields(cls)]
        tensorizer_args = cls(**{
            attr: getattr(args, attr)
            for attr in attrs if hasattr(args, attr)
        })
        return tensorizer_args


class TensorizerAgent:
    """
    A class for performing tensorizer deserializations specifically for
    aphrodite models using plaid_mode. Uses TensorizerArgs to configure the
    behavior of the TensorDeserializer when loading tensors from a serialized
    model. For deserializations of HuggingFace models, TensorDeserializer is
    instead used as an iterator directly in the func hf_model_weights_iterator
    in aphrodite/modeling/model_loader/weight_utils.py
    """

    def __init__(self, tensorizer_config: TensorizerConfig,
                 quant_config: QuantizationConfig, **extra_kwargs):
        if tensorizer_error_msg is not None:
            raise ImportError(
                "Tensorizer is not installed. Please install tensorizer "
                "to use this feature with "
                "`pip install aphrodite-engine[tensorizer]`. "
                "Error message: {}".format(tensorizer_error_msg))

        self.tensorizer_config = tensorizer_config
        self.tensorizer_args = (
            self.tensorizer_config._construct_tensorizer_args())
        self.extra_kwargs = extra_kwargs
        if extra_kwargs.get("quant_config", None) is not None:
            self.quant_config = extra_kwargs["quant_config"]
        else:
            self.quant_config = quant_config
        self.model = self._init_model()

    def _init_model(self):
        model_args = self.tensorizer_config.hf_config
        model_args.torch_dtype = self.tensorizer_config.dtype
        with no_init_or_tensor():
            return self.tensorizer_config.model_class(
                config=model_args,
                quant_config=self.quant_config,
                **self.extra_kwargs)

    def _resize_lora_embeddings(self):
        """Modify LoRA embedding layers to use bigger tensors
        to allow for adapter added tokens."""
        for child in self.model.modules():
            if (isinstance(child, VocabParallelEmbedding)
                    and child.weight.shape[0] <
                    child.num_embeddings_per_partition):
                new_weight = torch.empty(child.num_embeddings_per_partition,
                                         child.embedding_dim,
                                         dtype=child.weight.dtype,
                                         device=child.weight.device)
                new_weight[:child.weight.shape[0]].copy_(child.weight.data)
                new_weight[child.weight.shape[0]:].fill_(0)
                child.weight.data = new_weight

    def _check_tensors_on_meta_device(self):
        for tensor in self.model.state_dict().values():
            if tensor.device.type == 'meta':
                raise ValueError(
                    "The serialized model contains tensors on the meta device,"
                    " indicating that some tensors were not loaded properly."
                    " Please check that the parameters of the model being"
                    " specified match that of the serialized model, such as"
                    " its quantization.")

    def deserialize(self):
        """
        Deserialize the model using the TensorDeserializer. This method is
        specifically for Aphrodite models using tensorizer's plaid_mode.

        The deserializer makes use of tensorizer_args.stream_params
        to configure the behavior of the stream when loading tensors from a
        serialized model. The deserializer_params are used to configure the
        behavior of the TensorDeserializer when loading tensors themselves.
        Documentation on these params can be found in TensorizerArgs

        Returns:
            nn.Module: The deserialized model.
        """
        before_mem = get_mem_usage()
        start = time.perf_counter()
        with _read_stream(
                self.tensorizer_config.tensorizer_uri,
                **self.tensorizer_args.stream_params
        ) as stream, TensorDeserializer(
                stream,
                dtype=self.tensorizer_config.dtype,
                **self.tensorizer_args.deserializer_params) as deserializer:
            deserializer.load_into_module(self.model)
            end = time.perf_counter()

        total_bytes_str = convert_bytes(deserializer.total_tensor_bytes)
        duration = end - start
        per_second = convert_bytes(deserializer.total_tensor_bytes / duration)
        after_mem = get_mem_usage()
        deserializer.close()
        logger.info(f"Deserialized {total_bytes_str} in "
                    f"{end - start:0.2f}s, {per_second}/s")
        logger.info(f"Memory usage before: {before_mem}")
        logger.info(f"Memory usage after: {after_mem}")

        self._check_tensors_on_meta_device()
        self._resize_lora_embeddings()
        del self.model.aphrodite_tensorized_marker
        return self.model.eval()


def tensorizer_weights_iterator(
    tensorizer_args: "TensorizerArgs"
) -> Generator[Tuple[str, torch.Tensor], None, None]:
    logger.warning(
        "Deserializing HuggingFace models is not optimized for "
        "loading on Aphrodite, as tensorizer is forced to load to CPU. "
        "Consider deserializing a Aphrodite model instead for faster "
        "load times. See the examples/tensorize_aphrodite_model.py example "
        "script for serializing Aphrodite models.")

    deserializer_args = tensorizer_args.deserializer_params
    stream_params = tensorizer_args.stream_params
    stream = open_stream(tensorizer_args.tensorizer_uri, **stream_params)
    with TensorDeserializer(stream, **deserializer_args,
                            device="cpu") as state:
        for name, param in state.items():
            yield name, param
    del state


def is_aphrodite_tensorized(tensorizer_config: "TensorizerConfig") -> bool:
    """
    Infer if the model is a Aphrodite model by checking the weights for
    a Aphrodite tensorized marker.
    Args:
        tensorizer_config: The TensorizerConfig object containing the
            tensorizer_uri to the serialized model.
    Returns:
        bool: True if the model is a Aphrodite model, False otherwise.
    """
    tensorizer_args = tensorizer_config._construct_tensorizer_args()
    deserializer = TensorDeserializer(open_stream(
        tensorizer_args.tensorizer_uri, **tensorizer_args.stream_params),
                                      **tensorizer_args.deserializer_params,
                                      lazy_load=True)
    if tensorizer_config.aphrodite_tensorized:
        logger.warning(
            "Please note that newly serialized Aphrodite models are "
            "automatically inferred as Aphrodite models, so setting "
            "aphrodite_tensorized=True is only necessary for models serialized "
            "prior to this change.")
        return True
    if (".aphrodite_tensorized_marker" in deserializer):
        return True
    return False


def get_pretensorized_aphrodite_model(engine: "AphroditeEngine") -> nn.Module:
    model = (engine.model_executor.driver_worker.model_runner.model)
    model.register_parameter(
        "aphrodite_tensorized_marker",
        nn.Parameter(torch.tensor((1, ), device="meta"), requires_grad=False))
    return model


def serialize_aphrodite_model(engine: "AphroditeEngine",
                         tensorizer_config : TensorizerConfig,
                         encryption_key_path: Optional[str] = None) \
        -> nn.Module:

    model = get_pretensorized_aphrodite_model(engine)
    tensorizer_args = tensorizer_config._construct_tensorizer_args()
    encryption_params = None
    if encryption_key_path is not None:
        encryption_params = EncryptionParams.random()
        with _write_stream(encryption_key_path,
                           **tensorizer_args.stream_params) as stream:
            stream.write(encryption_params.key)

    with _write_stream(tensorizer_args.tensorizer_uri,
                       **tensorizer_args.stream_params) as stream:
        serializer = TensorSerializer(stream, encryption=encryption_params)
        serializer.write_module(model)
        serializer.close()
    logger.info("Successfully serialized model to "
                f"{str(tensorizer_args.tensorizer_uri)}")
    return model
