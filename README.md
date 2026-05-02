<h1 align="center">SWE Context Bench: A Benchmark for Context Learning in Coding</h1>


SWE-ContextBench is a benchmark created to evaluate how well programming agents, such as AI coding systems, can ***reuse past experience when solving new tasks***. It is built on top of existing datasets including [SWE-Bench Lite](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite), [SWE-Bench Multilingual](https://huggingface.co/datasets/SWE-bench/SWE-bench_Multilingual), and [SWE-Bench Verified](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified). The dataset contains 1,100 base tasks along with 376 related tasks that are derived from real dependency and reference relationships among GitHub issues and pull requests. These tasks are organized in a way that groups together problems with shared context, enabling the study of ***how effectively an agent can transfer knowledge across similar tasks***. The dataset spans 51 real-world GitHub repositories and covers 9 different programming languages. 

SWE-ContextBench is introduced as part of the research paper titled [SWE Context Bench: A Benchmark for Context Learning in Coding](https://arxiv.org/abs/2602.08316).

SWE-ContextBench dataset can be downloaded from [Hugging Face](https://huggingface.co/datasets/jiayuanz3/SWEContextBench).

 <div align="center"><img width="880" height="400" src="https://github.com/jiayuanz3/SWEContextBench/blob/main/assets/cover_image.png"></div>

## Run Evaluation
We provide pre-built [Docker images](https://hub.docker.com/r/jiayuanz3/swecontextbench/tags) for the related tasks.

Step1: Put your predictions in the `predictions` folder, with naming `{instance_id}_preds.json`

`{instance_id}_preds.json` follow the same data format convention as the SWE-Bench series datasets: 
```
{
  "instance_id": {
    "model_name_or_path": {model_name_or_path},
    "instance_id": {instance_id},
    "model_patch": {model_patch}
  }
}
```

Step2: Run
```
# Run only on "lite" subset
./evaluation.sh {run_id} lite

# Run on full dataset 
./evaluation.sh {run_id} full
```
, {run_id} can be any text, e.g., my_run_id


## 🚨 News
- 02-05-26. Code Uploaded and Dataset Released 👩‍💻
