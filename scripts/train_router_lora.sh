#!/usr/bin/env bash
# 训练 chitchat 五分类重训用的 router LoRA adapter（Phase 3，
# `docs/chitchat_intent_design.md`）。
#
# 超参数选择理由（都是本机实测出来的，不是照搬教程默认值）：
#
# --batch-size 1：本机曾在 batch-size 2 触发 Metal
#   `kIOGPUCommandBufferCallbackErrorOutOfMemory`（GPU working set 不够，
#   不是系统总内存不够——`top` 显示当时仍有 11G "unused"）。batch=1 时
#   peak mem 3.49GB，batch=2 时 5.6GB 但 tokens/sec 没有变快（约 21~23
#   token/s 两档几乎一样，说明这台机器上吞吐由单序列长度主导、批大小
#   带不来并行收益），所以选更省内存、同样快的 batch=1。
# --grad-checkpoint：不开的话 batch=2 都会 OOM，这是能跑起来的必要条件，
#   不是可选优化。
# --mask-prompt：prompt 部分（分类规则+工具列表+流程列表，每条样本几乎
#   相同的固定文本，~1400 token）不该算进 loss——不然会把"背下这段固定
#   规则文本"和"学会输出正确 JSON"这两件事的梯度信号混在一起，稀释后者。
# --iters 816：272 条训练样本 / batch=1 = 272 iters/epoch，816 = 整
#   3 个 epoch。选 3 epoch 是因为冒烟测试（10~8 iters）观察到 val loss
#   下降极快（1.0~1.2 -> 0.27~0.38），这份数据里绝大部分 token 是跨样本
#   相同的固定文本，模型收敛非常快，过多 epoch 有过拟合风险；3 epoch 是
#   "给够学习量、不做长跑"的折中，配合下面 --steps-per-eval 密集监控，
#   如果中途 val loss 掉头上升会在训练日志里看到。
# --learning-rate 1e-5：mlx_lm 默认值，冒烟测试显示这个学习率已经能在
#   几个 iter 内把 loss 打下来一半以上，没有调大的必要（调大在这种"prompt
#   高度重复、completion 很短"的数据形态下过拟合会更快）。
# --steps-per-eval 68：每 1/4 epoch 评估一次（272/4=68），密度足以在训练
#   日志里看出 val loss 曲线形状，用于判断要不要用某个中间 checkpoint
#   而不是最终权重。
# --val-batches -1：用满整个 valid 集（48 条），不采样子集——valid 集
#   本身已经很小，再采样会让每次评估的方差更大、看不清趋势。
#
# 用法：
#   set -a; source /Users/david/Documents/enterprise-qa-agent/.env; set +a
#   bash scripts/train_router_lora.sh 2>&1 | tee router_lora_data/adapters_v1/train_log.txt

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="/Users/david/Documents/enterprise-qa-agent/.venv/bin/mlx_lm.lora"

"$VENV_PY" \
  --model mlx-community/Qwen2.5-1.5B-Instruct-4bit \
  --train \
  --data "$REPO_ROOT/router_lora_data/mlx_chat_v1" \
  --mask-prompt \
  --batch-size 1 \
  --grad-checkpoint \
  --iters 816 \
  --val-batches -1 \
  --steps-per-report 34 \
  --steps-per-eval 68 \
  --save-every 136 \
  --adapter-path "$REPO_ROOT/router_lora_data/adapters_v1" \
  --seed 42
