"""Model adapter registration."""

import math
import sys
from typing import List, Optional
import warnings

if sys.version_info >= (3, 9):
    from functools import cache
else:
    from functools import lru_cache as cache

import psutil
import torch

from transformers import (
    AutoConfig,
    AutoModel,
    AutoModelForCausalLM,
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    LlamaTokenizer,
    LlamaForCausalLM,
    T5Tokenizer,
)

from fastchat.modules.gptq import GptqConfig, load_gptq_quantized
from fastchat.conversation import Conversation, get_conv_template
from fastchat.model.compression import load_compress_model
from fastchat.model.monkey_patch_non_inplace import (
    replace_llama_attn_with_non_inplace_operations,
)
from fastchat.utils import get_gpu_memory


class BaseAdapter:
    """The base and the default model adapter."""

    use_fast_tokenizer = False

    def match(self, model_path: str):
        return True

    def load_model(self, model_path: str, from_pretrained_kwargs: dict):
        tokenizer = AutoTokenizer.from_pretrained(
            model_path, use_fast=self.use_fast_tokenizer
        )
        model = AutoModelForCausalLM.from_pretrained(
            model_path, low_cpu_mem_usage=True, **from_pretrained_kwargs
        )
        return model, tokenizer

    def load_compress_model(self, model_path, device, torch_dtype):
        return load_compress_model(
            model_path, device, torch_dtype, use_fast=self.use_fast_tokenizer
        )

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("one_shot")


# A global registry for all model adapters
# TODO (lmzheng): make it a priority queue.
model_adapters: List[BaseAdapter] = []


def register_model_adapter(cls):
    """Register a model adapter."""
    model_adapters.append(cls())


@cache
def get_model_adapter(model_path: str) -> BaseAdapter:
    """Get a model adapter for a model_path."""
    for adapter in model_adapters:
        if adapter.match(model_path):
            return adapter
    raise ValueError(f"No valid model adapter for {model_path}")


def raise_warning_for_incompatible_cpu_offloading_configuration(
    device: str, load_8bit: bool, cpu_offloading: bool
):
    if cpu_offloading:
        if not load_8bit:
            warnings.warn(
                "The cpu-offloading feature can only be used while also using 8-bit-quantization.\n"
                "Use '--load-8bit' to enable 8-bit-quantization\n"
                "Continuing without cpu-offloading enabled\n"
            )
            return False
        if not "linux" in sys.platform:
            warnings.warn(
                "CPU-offloading is only supported on linux-systems due to the limited compatability with the bitsandbytes-package\n"
                "Continuing without cpu-offloading enabled\n"
            )
            return False
        if device != "cuda":
            warnings.warn(
                "CPU-offloading is only enabled when using CUDA-devices\n"
                "Continuing without cpu-offloading enabled\n"
            )
            return False
    return cpu_offloading


def load_model(
    model_path: str,
    device: str,
    num_gpus: int,
    max_gpu_memory: Optional[str] = None,
    load_8bit: bool = False,
    cpu_offloading: bool = False,
    gptq_config: Optional[GptqConfig] = None,
    debug: bool = False,
):
    """Load a model from Hugging Face."""

    # get model adapter
    adapter = get_model_adapter(model_path)

    # Handle device mapping
    cpu_offloading = raise_warning_for_incompatible_cpu_offloading_configuration(
        device, load_8bit, cpu_offloading
    )
    if device == "cpu":
        kwargs = {"torch_dtype": torch.float32}
    elif device == "cuda":
        kwargs = {"torch_dtype": torch.float16}
        if num_gpus != 1:
            kwargs["device_map"] = "auto"
            if max_gpu_memory is None:
                kwargs[
                    "device_map"
                ] = "sequential"  # This is important for not the same VRAM sizes
                available_gpu_memory = get_gpu_memory(num_gpus)
                kwargs["max_memory"] = {
                    i: str(int(available_gpu_memory[i] * 0.85)) + "GiB"
                    for i in range(num_gpus)
                }
            else:
                kwargs["max_memory"] = {i: max_gpu_memory for i in range(num_gpus)}
    elif device == "mps":
        kwargs = {"torch_dtype": torch.float16}
        # Avoid bugs in mps backend by not using in-place operations.
        replace_llama_attn_with_non_inplace_operations()
    elif device == "xpu":
        kwargs = {"torch_dtype": torch.bfloat16}
    else:
        raise ValueError(f"Invalid device: {device}")

    if cpu_offloading:
        # raises an error on incompatible platforms
        from transformers import BitsAndBytesConfig

        if "max_memory" in kwargs:
            kwargs["max_memory"]["cpu"] = (
                str(math.floor(psutil.virtual_memory().available / 2**20)) + "Mib"
            )
        kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_8bit_fp32_cpu_offload=cpu_offloading
        )
        kwargs["load_in_8bit"] = load_8bit
    elif load_8bit:
        if num_gpus != 1:
            warnings.warn(
                "8-bit quantization is not supported for multi-gpu inference."
            )
        else:
            return adapter.load_compress_model(
                model_path=model_path, device=device, torch_dtype=kwargs["torch_dtype"]
            )
    elif gptq_config and gptq_config.wbits < 16:
        return load_gptq_quantized(
            model_path,
            gptq_config,
            device=device,
        )

    # Load model
    adapter = get_model_adapter(model_path)
    model, tokenizer = adapter.load_model(model_path, kwargs)

    if (device == "cuda" and num_gpus == 1 and not cpu_offloading) or device == "mps":
        model.to(device)

    elif device == "xpu":
        try:
            import intel_extension_for_pytorch as ipex
        except ImportError:
            warnings.warn(
                "Intel Extension for PyTorch is not installed, but is required for xpu inference."
            )
        model.eval()
        model = model.to("xpu")
        model = torch.xpu.optimize(model, dtype=torch.bfloat16, inplace=True)

    if debug:
        print(model)

    return model, tokenizer


def get_conversation_template(model_path: str) -> Conversation:
    adapter = get_model_adapter(model_path)
    return adapter.get_default_conv_template(model_path)


def add_model_args(parser):
    parser.add_argument(
        "--model-path",
        type=str,
        default="lmsys/fastchat-t5-3b-v1.0",
        help="The path to the weights. This can be a local folder or a Hugging Face repo ID.",
    )
    parser.add_argument(
        "--device",
        type=str,
        choices=["cpu", "cuda", "mps", "xpu"],
        default="cuda",
        help="The device type",
    )
    parser.add_argument(
        "--gpus",
        type=str,
        default=None,
        help="A single GPU like 1 or multiple GPUs like 0,2",
    )
    parser.add_argument("--num-gpus", type=int, default=1)
    parser.add_argument(
        "--max-gpu-memory",
        type=str,
        help="The maximum memory per gpu. Use a string like '13Gib'",
    )
    parser.add_argument(
        "--load-8bit", action="store_true", help="Use 8-bit quantization"
    )
    parser.add_argument(
        "--cpu-offloading",
        action="store_true",
        help="Only when using 8-bit quantization: Offload excess weights to the CPU that don't fit on the GPU",
    )
    parser.add_argument(
        "--gptq-ckpt",
        type=str,
        default=None,
        help="Load quantized model. The path to the local GPTQ checkpoint.",
    )
    parser.add_argument(
        "--gptq-wbits",
        type=int,
        default=16,
        choices=[2, 3, 4, 8, 16],
        help="#bits to use for quantization",
    )
    parser.add_argument(
        "--gptq-groupsize",
        type=int,
        default=-1,
        help="Groupsize to use for quantization; default uses full row.",
    )
    parser.add_argument(
        "--gptq-act-order",
        action="store_true",
        help="Whether to apply the activation order GPTQ heuristic",
    )


def remove_parent_directory_name(model_path):
    """Remove parent directory name."""
    if model_path[-1] == "/":
        model_path = model_path[:-1]
    return model_path.split("/")[-1]


class VicunaAdapter(BaseAdapter):
    "Model adapater for vicuna-v1.1"

    def match(self, model_path: str):
        return "vicuna" in model_path

    def load_model(self, model_path: str, from_pretrained_kwargs: dict):
        tokenizer = AutoTokenizer.from_pretrained(model_path, use_fast=False)
        model = AutoModelForCausalLM.from_pretrained(
            model_path,
            low_cpu_mem_usage=True,
            **from_pretrained_kwargs,
        )
        self.raise_warning_for_old_weights(model)
        return model, tokenizer

    def get_default_conv_template(self, model_path: str) -> Conversation:
        if "v0" in remove_parent_directory_name(model_path):
            return get_conv_template("one_shot")
        return get_conv_template("vicuna_v1.1")

    def raise_warning_for_old_weights(self, model):
        if isinstance(model, LlamaForCausalLM) and model.model.vocab_size > 32000:
            warnings.warn(
                "\nYou are probably using the old Vicuna-v0 model, "
                "which will generate unexpected results with the "
                "current fastchat.\nYou can try one of the following methods:\n"
                "1. Upgrade your weights to the new Vicuna-v1.1: https://github.com/lm-sys/FastChat#vicuna-weights.\n"
                "2. Use the old conversation template by `python3 -m fastchat.serve.cli --model-path /path/to/vicuna-v0 --conv-template conv_one_shot`\n"
                "3. Downgrade fschat to fschat==0.1.10 (Not recommonded).\n"
            )


class T5Adapter(BaseAdapter):
    """The model adapter for lmsys/fastchat-t5-3b-v1.0"""

    def match(self, model_path: str):
        return "t5" in model_path

    def load_model(self, model_path: str, from_pretrained_kwargs: dict):
        tokenizer = T5Tokenizer.from_pretrained(model_path, use_fast=False)
        model = AutoModelForSeq2SeqLM.from_pretrained(
            model_path, low_cpu_mem_usage=True, **from_pretrained_kwargs
        )
        return model, tokenizer


class KoalaAdapter(BaseAdapter):
    """The model adapter for koala"""

    def match(self, model_path: str):
        return "koala" in model_path

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("koala_v1")


class AlpacaAdapter(BaseAdapter):
    """The model adapter for alpaca."""

    def match(self, model_path: str):
        return "alpaca" in model_path.lower()

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("alpaca")


class ChatGLMAdapter(BaseAdapter):
    """The model adapter for THUDM/chatglm-6b"""

    def match(self, model_path: str):
        return "chatglm" in model_path

    def load_model(self, model_path: str, from_pretrained_kwargs: dict):
        tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
        model = AutoModel.from_pretrained(
            model_path, trust_remote_code=True, **from_pretrained_kwargs
        )
        return model, tokenizer


class DollyV2Adapter(BaseAdapter):
    """The model adapter for databricks/dolly-v2-12b"""

    use_fast_tokenizer = True

    def match(self, model_path: str):
        return "dolly-v2" in model_path

    def load_model(self, model_path: str, from_pretrained_kwargs: dict):
        tokenizer = AutoTokenizer.from_pretrained(model_path, use_fast=True)
        model = AutoModelForCausalLM.from_pretrained(
            model_path,
            low_cpu_mem_usage=True,
            **from_pretrained_kwargs,
        )
        # 50277 means "### End"
        tokenizer.eos_token_id = 50277
        return model, tokenizer

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("dolly_v2")


class OasstPythiaAdapter(BaseAdapter):
    """The model adapter for OpenAssistant/oasst-sft-1-pythia-12b"""

    use_fast_tokenizer = True

    def match(self, model_path: str):
        return "oasst" in model_path and "pythia" in model_path

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("oasst_pythia")


class StableLMAdapter(BaseAdapter):
    """The model adapter for StabilityAI/stablelm-tuned-alpha-7b"""

    use_fast_tokenizer = True

    def match(self, model_path: str):
        return "stablelm" in model_path

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("stablelm")


class MPTAdapter(BaseAdapter):
    """The model adapter for mosaicml/mpt-7b-chat"""

    use_fast_tokenizer = True

    def match(self, model_path: str):
        return "mpt" in model_path

    def load_model(self, model_path: str, from_pretrained_kwargs: dict):
        model = AutoModelForCausalLM.from_pretrained(
            model_path,
            low_cpu_mem_usage=True,
            trust_remote_code=True,
            max_seq_len=8192,
            **from_pretrained_kwargs,
        )
        tokenizer = AutoTokenizer.from_pretrained(
            model_path, trust_remote_code=True, use_fast=True
        )
        return model, tokenizer

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("mpt")


class BaizeAdapter(BaseAdapter):
    """The model adapter for project-baize/baize-lora-7B"""

    def match(self, model_path: str):
        return "baize" in model_path

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("baize")


class RwkvAdapter(BaseAdapter):
    """The model adapter for BlinkDL/RWKV-4-Raven"""

    use_fast_tokenizer = True

    def match(self, model_path: str):
        return "RWKV-4" in model_path

    def load_model(self, model_path: str, from_pretrained_kwargs: dict):
        from fastchat.model.rwkv_model import RwkvModel

        model = RwkvModel(model_path)
        tokenizer = AutoTokenizer.from_pretrained(
            "EleutherAI/pythia-160m", use_fast=True
        )
        return model, tokenizer

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("rwkv")


class OpenBuddyAdapter(BaseAdapter):
    """The model adapter for OpenBuddy/openbuddy-7b-v1.1-bf16-enc"""

    def match(self, model_path: str):
        return "openbuddy" in model_path

    def load_model(self, model_path: str, from_pretrained_kwargs: dict):
        if "-bf16" in model_path:
            from_pretrained_kwargs["torch_dtype"] = torch.bfloat16
            warnings.warn(
                "## This is a bf16(bfloat16) variant of OpenBuddy. Please make sure your GPU supports bf16."
            )
        model = LlamaForCausalLM.from_pretrained(
            model_path, low_cpu_mem_usage=True, **from_pretrained_kwargs
        )
        tokenizer = LlamaTokenizer.from_pretrained(model_path)
        return model, tokenizer

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("openbuddy")


class PhoenixAdapter(BaseAdapter):
    """The model adapter for FreedomIntelligence/phoenix-inst-chat-7b"""

    use_fast_tokenizer = True

    def match(self, model_path: str):
        return "phoenix" in model_path

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("phoenix")


class ChatGPTAdapter(BaseAdapter):
    """The model adapter for ChatGPT."""

    def match(self, model_path: str):
        return model_path == "gpt-3.5-turbo" or model_path == "gpt-4"

    def load_model(self, model_path: str, from_pretrained_kwargs: dict):
        raise NotImplementedError()

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("chatgpt")


class ClaudeAdapter(BaseAdapter):
    """The model adapter for Claude."""

    def match(self, model_path: str):
        return model_path in ["claude-v1", "claude-instant-v1"]

    def load_model(self, model_path: str, from_pretrained_kwargs: dict):
        raise NotImplementedError()

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("claude")


class BardAdapter(BaseAdapter):
    """The model adapter for Bard."""

    def match(self, model_path: str):
        return model_path == "bard"

    def load_model(self, model_path: str, from_pretrained_kwargs: dict):
        raise NotImplementedError()

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("bard")


class PaLM2Adapter(BaseAdapter):
    """The model adapter for PaLM2."""

    def match(self, model_path: str):
        return model_path == "palm-2"

    def load_model(self, model_path: str, from_pretrained_kwargs: dict):
        raise NotImplementedError()

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("bard")


class BiLLaAdapter(BaseAdapter):
    """The model adapter for BiLLa."""

    def match(self, model_path: str):
        return "billa" in model_path.lower()

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("billa")


class RedPajamaINCITEAdapter(BaseAdapter):
    """The model adapter for RedPajama INCITE."""

    def match(self, model_path: str):
        return "redpajama-incite" in model_path.lower()

    def load_model(self, model_path: str, from_pretrained_kwargs: dict):
        tokenizer = AutoTokenizer.from_pretrained(model_path)  # no use_fast=False
        model = AutoModelForCausalLM.from_pretrained(
            model_path,
            low_cpu_mem_usage=True,
            **from_pretrained_kwargs,
        )
        return model, tokenizer

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("redpajama-incite")


class H2OGPTAdapter(BaseAdapter):
    """The model adapter for h2oGPT."""

    def match(self, model_path: str):
        return "h2ogpt" in model_path.lower()

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("h2ogpt")


class SnoozyAdapter(BaseAdapter):
    """The model adapter for nomic-ai/gpt4all-13b-snoozy"""

    def match(self, model_path: str):
        return "gpt4all" in model_path and "snoozy" in model_path

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("snoozy")


class WizardLMAdapter(BaseAdapter):
    """The model adapter for WizardLM/WizardLM-13B-V1.0"""

    def match(self, model_path: str):
        return "wizardlm" in model_path.lower()

    def get_default_conv_template(self, model_path: str) -> Conversation:
        model_path = model_path.lower()
        if "13b" in model_path or "30b" in model_path:
            return get_conv_template("vicuna_v1.1")
        else:
            # TODO: use the recommended template for 7B
            # (https://huggingface.co/WizardLM/WizardLM-13B-V1.0)
            return get_conv_template("one_shot")


class ManticoreAdapter(BaseAdapter):
    """The model adapter for Manticore."""

    def match(self, model_path: str):
        return "manticore" in model_path.lower()

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("manticore")


class GuanacoAdapter(BaseAdapter):
    """The model adapter for timdettmers/guanaco-33b-merged"""

    def match(self, model_path: str):
        return "guanaco" in model_path

    def load_model(self, model_path: str, from_pretrained_kwargs: dict):
        tokenizer = AutoTokenizer.from_pretrained(model_path, use_fast=False)
        model = AutoModelForCausalLM.from_pretrained(
            model_path, low_cpu_mem_usage=True, **from_pretrained_kwargs
        )
        # Fix a bug in tokenizer config
        tokenizer.eos_token_id = model.config.eos_token_id
        return model, tokenizer

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("zero_shot")


class CamelAdapter(BaseAdapter):
    """The model adapter for camel"""

    def match(self, model_path: str):
        return "camel" in model_path

    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template("vicuna_v1.1")

class FalconAdapter(BaseAdapter):
    """The model adapter for falcon."""

    def match(self, model_path: str):
        return "falcon" in model_path.lower()

    def load_model(self, model_path: str, from_pretrained_kwargs: dict):
        config = AutoConfig.from_pretrained(model_path,
                                            trust_remote_code=True,)
        
        # from_pretrained_kwargs['torch_dtype'] = torch.bfloat16
        model = AutoModelForCausalLM.from_pretrained(
            model_path,
            config=config,
            low_cpu_mem_usage=True,
            trust_remote_code=True,
            torch_dtype=torch.bfloat16,
        )
        tokenizer = AutoTokenizer.from_pretrained(model_path)
        # in falcon tokenizer config and speical config there is not any pad token
        # setting `pad_token_id` to 9, which corresponde to speical token '>>SUFFIX<<' 
        tokenizer.pad_token_id = 9
        return model, tokenizer
    def get_default_conv_template(self, model_path: str) -> Conversation:
        return get_conv_template('falcon')



# Note: the registration order matters.
# The one registered earlier has a higher matching priority.
register_model_adapter(VicunaAdapter)
register_model_adapter(T5Adapter)
register_model_adapter(KoalaAdapter)
register_model_adapter(AlpacaAdapter)
register_model_adapter(ChatGLMAdapter)
register_model_adapter(DollyV2Adapter)
register_model_adapter(OasstPythiaAdapter)
register_model_adapter(StableLMAdapter)
register_model_adapter(BaizeAdapter)
register_model_adapter(RwkvAdapter)
register_model_adapter(OpenBuddyAdapter)
register_model_adapter(PhoenixAdapter)
register_model_adapter(BardAdapter)
register_model_adapter(PaLM2Adapter)
register_model_adapter(ChatGPTAdapter)
register_model_adapter(ClaudeAdapter)
register_model_adapter(MPTAdapter)
register_model_adapter(BiLLaAdapter)
register_model_adapter(RedPajamaINCITEAdapter)
register_model_adapter(H2OGPTAdapter)
register_model_adapter(SnoozyAdapter)
register_model_adapter(WizardLMAdapter)
register_model_adapter(ManticoreAdapter)
register_model_adapter(GuanacoAdapter)
register_model_adapter(CamelAdapter)
register_model_adapter(FalconAdapter)


# After all adapters, try the default base adapter.
register_model_adapter(BaseAdapter)
