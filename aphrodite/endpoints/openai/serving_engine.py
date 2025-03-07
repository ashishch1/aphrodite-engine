import asyncio
import json
from dataclasses import dataclass
from http import HTTPStatus
from typing import Dict, List, Optional, Tuple, Union

from loguru import logger
from pydantic import conint

from aphrodite.common.sequence import Logprob
from aphrodite.endpoints.openai.protocol import (
    ChatCompletionRequest, CompletionRequest, EmbeddingRequest, ErrorResponse,
    LogProbs, ModelCard, ModelList, ModelPermission, Prompt)
from aphrodite.engine.async_aphrodite import AsyncAphrodite
from aphrodite.lora.request import LoRARequest
from aphrodite.transformers_utils.tokenizer import get_tokenizer


@dataclass
class LoRA:
    name: str
    local_path: str


class OpenAIServing:

    def __init__(self,
                 engine: AsyncAphrodite,
                 served_model_names: List[str],
                 lora_modules=Optional[List[LoRA]]):
        self.engine = engine
        self.served_model_names = served_model_names
        if lora_modules is None:
            self.lora_requests = []
        else:
            self.lora_requests = [
                LoRARequest(
                    lora_name=lora.name,
                    lora_int_id=i,
                    lora_local_path=lora.local_path,
                ) for i, lora in enumerate(lora_modules, start=1)
            ]

        self.max_model_len = 0
        self.tokenizer = None

        try:
            event_loop = asyncio.get_running_loop()
        except RuntimeError:
            event_loop = None

        if event_loop is not None and event_loop.is_running():
            # If the current is instanced by Ray Serve,
            # there is already a running event loop
            event_loop.create_task(self._post_init())
        else:
            # When using single Aphrodite without engine_use_ray
            asyncio.run(self._post_init())

    async def _post_init(self):
        engine_model_config = await self.engine.get_model_config()
        self.max_model_len = engine_model_config.max_model_len

        # A separate tokenizer to map token IDs to strings.
        self.tokenizer = get_tokenizer(
            engine_model_config.tokenizer,
            tokenizer_mode=engine_model_config.tokenizer_mode,
            trust_remote_code=engine_model_config.trust_remote_code,
            revision=engine_model_config.revision,
            truncation_side="left")

    async def show_available_models(self) -> ModelList:
        """Show available models. Right now we only have one model."""
        model_cards = [
            ModelCard(id=served_model_name,
                      root=self.served_model_names[0],
                      permission=[ModelPermission()])
            for served_model_name in self.served_model_names
        ]
        lora_cards = [
            ModelCard(id=lora.lora_name,
                      root=self.served_model_names[0],
                      permission=[ModelPermission()])
            for lora in self.lora_requests
        ]
        model_cards.extend(lora_cards)
        return ModelList(data=model_cards)

    async def tokenize(self, prompt: Prompt):
        """Tokenize a given prompt."""
        tokenized_prompt = self.tokenizer.tokenize(prompt.prompt)
        token_ids = self.tokenizer.convert_tokens_to_ids(tokenized_prompt)
        return {"value": len(tokenized_prompt), "ids": token_ids}

    async def detokenize(self, token_ids: List[int]):
        """Detokenize a given list of token IDs."""
        tokens = self.tokenizer.convert_ids_to_tokens(token_ids)
        detokenized_text = self.tokenizer.convert_tokens_to_string(tokens)
        return {"value": detokenized_text}

    def _create_logprobs(
        self,
        token_ids: List[int],
        top_logprobs: Optional[List[Optional[Dict[int, Logprob]]]] = None,
        num_output_top_logprobs: Optional[int] = None,
        initial_text_offset: int = 0,
    ) -> LogProbs:
        """Create OpenAI-style logprobs."""
        logprobs = LogProbs()
        last_token_len = 0
        if num_output_top_logprobs:
            logprobs.top_logprobs = []

        for i, token_id in enumerate(token_ids):
            step_top_logprobs = top_logprobs[i]
            if step_top_logprobs is None:
                token = self.tokenizer.decode(token_id)
                logprobs.tokens.append(token)
                logprobs.token_logprobs.append(None)
                logprobs.top_logprobs.append(None)
            else:
                token_logprob = step_top_logprobs[token_id].logprob
                token = step_top_logprobs[token_id].decoded_token
                logprobs.tokens.append(token)
                logprobs.token_logprobs.append(token_logprob)

                if num_output_top_logprobs:
                    logprobs.top_logprobs.append({
                        # Convert float("-inf") to the
                        # JSON-serializable float that OpenAI uses
                        p.decoded_token: max(p.logprob, -9999.0)
                        for i, p in step_top_logprobs.items()
                    } if step_top_logprobs else None)

            # TODO: Check if this is still needed
            if logprobs.top_logprobs:
                logprobs.top_logprobs = [{
                    k: v if v > -1000 else -1000
                    for k, v in top_logprob.items()
                } for top_logprob in logprobs.top_logprobs
                                         if top_logprob is not None
                                         ]  # noqa: E501

            if len(logprobs.text_offset) == 0:
                logprobs.text_offset.append(initial_text_offset)
            else:
                logprobs.text_offset.append(logprobs.text_offset[-1] +
                                            last_token_len)
            last_token_len = len(token)
        return logprobs

    def create_error_response(
            self,
            message: str,
            err_type: str = "BadRequestError",
            status_code: HTTPStatus = HTTPStatus.BAD_REQUEST) -> ErrorResponse:
        return ErrorResponse(message=message,
                             type=err_type,
                             code=status_code.value)

    def create_streaming_error_response(
            self,
            message: str,
            err_type: str = "BadRequestError",
            status_code: HTTPStatus = HTTPStatus.BAD_REQUEST) -> str:
        json_str = json.dumps({
            "error":
            self.create_error_response(message=message,
                                       err_type=err_type,
                                       status_code=status_code).model_dump()
        })
        return json_str

    async def _check_model(self, request) -> Optional[ErrorResponse]:
        if request.model in self.served_model_names:
            return
        if request.model in [lora.lora_name for lora in self.lora_requests]:
            return
        return self.create_error_response(
            message=f"The model `{request.model}` does not exist.",
            err_type="NotFoundError",
            status_code=HTTPStatus.NOT_FOUND)

    def add_lora(self, lora: LoRA):
        if lora.name in [
                existing_lora.lora_name for existing_lora in self.lora_requests
        ]:
            logger.error(f"LoRA with name {lora.name} already exists.")
            return
        self.lora_requests.append(
            LoRARequest(
                lora_name=lora.name,
                lora_int_id=len(self.lora_requests) + 1,
                lora_local_path=lora.local_path,
            ))

    def remove_lora(self, lora_name: str):
        self.lora_requests = [
            lora for lora in self.lora_requests if lora.lora_name != lora_name
        ]

    def _maybe_get_lora(self, request) -> Optional[LoRARequest]:
        if request.model in self.served_model_names:
            return
        for lora in self.lora_requests:
            if request.model == lora.lora_name:
                return lora
        # if _check_model has been called earlier, this will be unreachable
        raise ValueError("The model `{request.model}` does not exist.")

    def _validate_prompt_and_tokenize(
        self,
        request: Union[ChatCompletionRequest, CompletionRequest,
                       EmbeddingRequest],
        prompt: Optional[str] = None,
        prompt_ids: Optional[List[int]] = None,
        truncate_prompt_tokens: Optional[conint(ge=1)] = None
    ) -> Tuple[List[int], str]:
        if not (prompt or prompt_ids):
            raise ValueError("Either prompt or prompt_ids should be provided.")
        if (prompt and prompt_ids):
            raise ValueError(
                "Only one of prompt or prompt_ids should be provided.")

        if prompt_ids is None:
            tokenizer_kwargs = {} if truncate_prompt_tokens is None else {
                "truncation": True,
                "max_length": truncate_prompt_tokens,
            }
            input_ids = self.tokenizer(prompt, **tokenizer_kwargs).input_ids
        elif truncate_prompt_tokens is not None:
            input_ids = prompt_ids[-truncate_prompt_tokens:]
        else:
            input_ids = prompt_ids

        input_text = prompt if prompt is not None else self.tokenizer.decode(
            prompt_ids)
        token_num = len(input_ids)

        # Note: EmbeddingRequest doesn't have max_tokens
        if isinstance(request, EmbeddingRequest):
            if token_num > self.max_model_len:
                raise ValueError(
                    f"This model's maximum context length is "
                    f"{self.max_model_len} tokens. However, you requested "
                    f"{token_num} tokens in the input for embedding "
                    f"generation. Please reduce the length of the input.", )
            return input_ids, input_text

        if request.max_tokens is None:
            request.max_tokens = self.max_model_len - token_num

        if token_num + request.max_tokens > self.max_model_len:
            raise ValueError(
                f"This model's maximum context length is "
                f"{self.max_model_len} tokens. However, you requested "
                f"{request.max_tokens + token_num} tokens "
                f"({token_num} in the messages, "
                f"{request.max_tokens} in the completion). "
                f"Please reduce the length of the messages or completion.", )
        else:
            return input_ids, input_text
