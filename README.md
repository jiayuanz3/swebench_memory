# Evaluation

Step1: Put your predictions in the `predictions` folder, with naming `{instance_id}_preds.json`

Step2: Run 
```
# Run only on "lite" subset
./evaluation.sh {run_id} lite

# Run on full dataset 
./evaluation.sh {run_id} full
```
, {run_id} can be any text, e.g., my_run_id
