#!/usr/bin/env bash
# Phase 3 收尾：把训练好的 LoRA adapter 融合进基座模型、转成 GGUF、注册成一个
# **新的** Ollama 模型名（绝不覆盖 qwen2.5-1.5b-router，那是生产在用的模型）。
#
# 分三步，每步都是本次任务里实测验证过、能跑通的路径：
#
# 1. `mlx_lm.fuse --dequantize`：把 LoRA 权重融合进基座权重，同时反量化成
#    fp16——**必须反量化**，因为下一步 `mlx_lm.gguf.convert_to_gguf` 显式拒绝
#    量化模型（`raise NotImplementedError("Conversion of quantized models is
#    not yet supported.")`，见 mlx_lm/gguf.py:272）。
#    ⚠️ 这里**不能**加 `mlx_lm.fuse` 自带的 `--export-gguf`——它对
#    `model_type` 有白名单（`llama`/`mixtral`/`mistral`，见 mlx_lm/fuse.py:95），
#    Qwen2 不在里面，加了会直接抛 `ValueError: Model type qwen2 not
#    supported for GGUF conversion.`（本次实测确认，不是猜的）。
#    `mlx_lm.fuse` 的 `save()` 产出的是标准 HF 目录结构（safetensors +
#    config.json + `tokenizer.save_pretrained()` 落的 tokenizer 文件），
#    可以直接喂给下一步的 llama.cpp 转换脚本。
#
# 2. `scripts/vendor/convert_hf_to_gguf.py`（llama.cpp 官方脚本，tag b5900，
#    2026-05 前后与本机已装的 `gguf` pip 包 0.19.0 同期，本次实测两者能配合
#    工作）：把上一步的 HF 目录转成 GGUF。本机没有 llama.cpp 的编译产物、也
#    没有 `llama-quantize` 二进制，所以量化档位限定在这个脚本自带的纯 Python
#    量化实现能覆盖的范围（`--outtype q8_0`）——不是原版 `qwen2.5-1.5b-router`
#    用的 Q4_K_M（那需要 `llama-quantize` 这个 C++ 工具，本机没有），Q8_0
#    体积更大但精度损失更小，且是 Ollama/llama.cpp 生态里广泛支持的标准档位，
#    不需要额外转换。
#
# 3. `ollama create`：**不手写 TEMPLATE**——GGUF 转换会把 HF tokenizer_config.json
#    里的 `chat_template`（Qwen2.5-Instruct 标准 ChatML 模板）写进 GGUF 自己的
#    元数据，Ollama 导入时会自动识别使用，这一点已经用原始 `qwen2.5-1.5b-router`
#    模型实测验证过（`ollama show --modelfile` 显示的 `TEMPLATE {{ .Prompt }}`
#    只是占位显示，`ollama show --template` 才显示真正生效的、来自 GGUF 元数据
#    的完整 Jinja 模板；非 raw 模式的 `/api/generate` 请求确实经过了这层模板，
#    跟 raw 模式对照测过，输出质量天差地别）。**新模型名绝不能是
#    `qwen2.5-1.5b-router`**——那会覆盖生产模型，没有回滚路径。
#
# 用法：
#   set -a; source /Users/david/Documents/enterprise-qa-agent/.env; set +a
#   bash scripts/fuse_convert_register_router_lora.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_BIN="/Users/david/Documents/enterprise-qa-agent/.venv/bin"
NEW_MODEL_NAME="qwen2.5-1.5b-router-chitchat-v1"

FUSED_DIR="$REPO_ROOT/router_lora_data/fused_hf_v1"
GGUF_PATH="$REPO_ROOT/router_lora_data/router_chitchat_v1.q8_0.gguf"
MODELFILE_PATH="$REPO_ROOT/router_lora_data/Modelfile.chitchat_v1"

echo "=== Step 1/3: mlx_lm.fuse (dequantize) ==="
"$VENV_BIN/mlx_lm.fuse" \
  --model mlx-community/Qwen2.5-1.5B-Instruct-4bit \
  --adapter-path "$REPO_ROOT/router_lora_data/adapters_v1" \
  --save-path "$FUSED_DIR" \
  --dequantize

echo "=== Step 2/3: convert_hf_to_gguf.py -> Q8_0 GGUF ==="
NO_LOCAL_GGUF=1 "$VENV_BIN/python" "$REPO_ROOT/scripts/vendor/convert_hf_to_gguf.py" \
  "$FUSED_DIR" \
  --outfile "$GGUF_PATH" \
  --outtype q8_0

echo "=== Step 3/3: ollama create (NOT overwriting qwen2.5-1.5b-router) ==="
cat > "$MODELFILE_PATH" <<EOF
FROM $GGUF_PATH
PARAMETER temperature 0
EOF

ollama create "$NEW_MODEL_NAME" -f "$MODELFILE_PATH"

echo "=== Done. New model registered as: $NEW_MODEL_NAME ==="
echo "    (production 'qwen2.5-1.5b-router' was NOT touched)"
ollama show "$NEW_MODEL_NAME" || true
