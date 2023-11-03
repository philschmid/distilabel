import logging
import os
import warnings
from functools import cached_property
from typing import TYPE_CHECKING, Any, Callable, Dict, Final, List, Tuple, Union

import openai
from openai.error import APIError, RateLimitError, ServiceUnavailableError, Timeout
from tenacity import (
    after_log,
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from ultralabel.llm.base import LLM
from ultralabel.tasks.utils import Prompt

if TYPE_CHECKING:
    from ultralabel.tasks.base import Task


_OPENAI_API_RETRY_ON_EXCEPTIONS = (
    APIError,
    Timeout,
    RateLimitError,
    ServiceUnavailableError,
)
_OPENAI_API_STOP_AFTER_ATTEMPT = 6
_OPENAI_API_WAIT_RANDOM_EXPONENTIAL_MULTIPLIER = 1
_OPENAI_API_WAIT_RANDOM_EXPONENTIAL_MAX = 10

logger: Final = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class OpenAILLM(LLM):
    def __init__(
        self,
        task: "Task",
        model: str = "gpt-3.5-turbo",
        openai_api_key: Union[str, None] = None,
        max_new_tokens: int = 128,
        temperature: float = 0.7,
        num_threads: Union[int, None] = None,
        formatting_fn: Union[Callable[..., str], None] = None,
    ) -> None:
        super().__init__(
            task=task,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            num_threads=num_threads,
            formatting_fn=formatting_fn,
        )

        openai.api_key = openai_api_key or os.environ.get("OPENAI_API_KEY")
        assert (
            model in self.available_models
        ), f"Provided `model` is not available in your OpenAI account, available models are {self.available_models}"
        self.model = model

        assert (
            openai.api_key is not None
        ), "Either the `openai_api_key` arg or the `OPENAI_API_KEY` environment variable must be set to use the OpenAI API."

    @cached_property
    def available_models(self) -> List[str]:
        return [
            model["id"]
            for model in openai.Model.list().get("data", [])
            if model.get("id") is not None
        ]

    @retry(
        retry=retry_if_exception_type(_OPENAI_API_RETRY_ON_EXCEPTIONS),
        stop=stop_after_attempt(_OPENAI_API_STOP_AFTER_ATTEMPT),
        wait=wait_random_exponential(
            multiplier=_OPENAI_API_WAIT_RANDOM_EXPONENTIAL_MULTIPLIER,
            max=_OPENAI_API_WAIT_RANDOM_EXPONENTIAL_MAX,
        ),
        before_sleep=before_sleep_log(logger, logging.INFO),
        after=after_log(logger, logging.INFO),
    )
    def _chat_completion_with_backoff(self, **kwargs: Any) -> Any:
        return openai.ChatCompletion.create(**kwargs)

    def _generate(
        self,
        input: Dict[str, Any],
        num_generations: int = 1,
    ) -> Tuple[Any, List[Any]]:
        # TODO(alvarobartt): move this responsibility to the `Task` class
        prompt = self.task.generate_prompt(**input)
        if not isinstance(prompt, Prompt) and self.formatting_fn is not None:
            warnings.warn(
                f"The method `generate_prompt` is not returning a `Prompt` class but a prompt of `type={type(prompt)}`, meaning that a pre-formatting has already been applied in the `task.generate_prompt` method, so the usage of a `formatting_fn` is discouraged.",
                UserWarning,
                stacklevel=2,
            )
            prompt = self.formatting_fn(prompt)
        elif isinstance(prompt, Prompt) and self.formatting_fn is None:
            prompt = prompt.format_as(format="openai")
        if not isinstance(prompt, list):
            raise ValueError(
                f"The provided `prompt={prompt}` is of `type={type(prompt)}`, but it must be a `list`, make sure that `task.generate_prompt` returns a `list` or that the `formatting_fn` formats the prompt as a `list`, where each item follows OpenAI's format of `{'role': ..., 'content': ...}`."
            )
        raw_response = self._chat_completion_with_backoff(
            model=self.model,
            messages=prompt,
            n=num_generations,
            temperature=self.temperature,
            max_tokens=self.max_new_tokens,
        )
        try:
            parsed_response = [
                self.task.parse_output(choice["message"]["content"].strip())
                for choice in raw_response["choices"]
            ]
        except Exception as e:
            warnings.warn(
                f"Error parsing OpenAI response: {e}", UserWarning, stacklevel=2
            )
            parsed_response = []
        return raw_response.to_dict_recursive(), parsed_response