# I Tested DeepSeek V4.1 Flash on a $250,000 GPU Rig (1 MILLION Tokens!)

Automation Avenue · 2026-09-16 · 9:25 · [watch](https://www.youtube.com/watch?v=FDSkt-6iMnA)

Transcript: captions, 1652 words. Brief: ollama gemma4:26b-a4b-it-qat, 2026-09-20.

---

## The point
The speaker tests the DeepSeek V4.1 Flash model on a high-end GPU rig with different context window sizes.

## Key points
- [00:04] The rig uses eight Nvidia H200 GPUs.
- [00:34] DeepSeek V4.1 Flash has 552 billion parameters.
- [00:34] The model supports a 1 million token context window.
- [03:41] The model generates 127 tokens per second at 32,000 context.
- [04:42] The model generates 665 tokens per second with 8 concurrent requests.
- [05:45] The model generates 90 tokens per second at 250,000 context.
- [06:47] The model generates 240 tokens per second with 8 concurrent requests at 250,000 context.
- [08:19] The model generates 2 tokens per second at 1 million context.
- [08:50] The model generates 66 tokens per second at 1 million context.

## Facts and numbers
- $250,000: the approximate cost of the GPU rig
- $30,000 to $35,000: the price of one Nvidia H200 GPU
- 8: the number of Nvidia H200 SXM cards
- 1 TB: the total VRAM in the rig
- 2 TB: the total system RAM
- 552 billion: the number of parameters in DeepSeek V4.1 Flash
- 1 million: the maximum context window in tokens
- 32,000: the first context window size tested
- 250,000: the second context window size tested
- 1,000,000: the third context window size tested
- 127 tokens per second: the speed at 32,000 context (single request)
- 665 tokens per second: the speed at 32,000 context (8 concurrent requests)
- 90 tokens per second: the speed at 250,000 context (single request)
- 240 tokens per second: the speed at 250,000 context (8 concurrent requests)
- 66 tokens per second: the speed at 1 million context (8 concurrent requests)
- 16 to 17 seconds: the median time to first token at 1 million context

## For me
- nothing relevant

## Doubt
- The speaker guesses that the model is better than Claude Opus 5 and GPT 5.6.
