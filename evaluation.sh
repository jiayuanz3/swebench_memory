#!/bin/bash
# Batch evaluation script for multiple SWE-bench instances

set -e

# Configuration
RUN_ID="${1:-batch_baseline}"
PREDICTIONS_DIR="${3:-predictions}"

# Select instances directory based on optional second argument
case "${2:-}" in
    lite)         INSTANCES_DIR="cases/SWEContextBench Lite" ;;
    multilingual) INSTANCES_DIR="cases/SWEContextBench Multilingual" ;;
    verified)     INSTANCES_DIR="cases/SWEContextBench Verified" ;;
    full)         INSTANCES_DIR="cases/SWEContextBench Full" ;;
    "")           INSTANCES_DIR="cases" ;;
    *)            echo "Unknown subset '${2}'. Use: lite, multilingual, verified, full, or omit for all."; exit 1 ;;
esac
DATASET_FILE="batch_dataset.json"
PREDICTIONS_FILE="batch_predictions.json"

echo "============================================================"
echo "SWE-bench Batch Evaluation"
echo "============================================================"
echo "Run ID: $RUN_ID"
echo "Instances directory: $INSTANCES_DIR"
echo "Predictions directory: $PREDICTIONS_DIR"
echo ""

# Step 1: Combine all instances and predictions
echo "Step 1: Combining files..."
python3 -m swebench_memory.harness.combine_instances \
    --instances "$INSTANCES_DIR" \
    --predictions "$PREDICTIONS_DIR" \
    --dataset-output "$DATASET_FILE" \
    --predictions-output "$PREDICTIONS_FILE"

if [ ! -f "$DATASET_FILE" ] || [ ! -f "$PREDICTIONS_FILE" ]; then
    echo "✗ Failed to create combined files"
    exit 1
fi

echo ""
echo "============================================================"
echo "Step 2: Running evaluation..."
echo "============================================================"
echo ""

# Ensure base Docker image exists
if ! docker images jiayuanz3/memory:base -q | grep -q .; then
    echo "  → Base image not found, pulling from Docker Hub..."
    docker pull jiayuanz3/memory:base
    if [ $? -eq 0 ]; then
        echo "  ✓ Base image found: jiayuanz3/memory:base"
    else
        echo "  ✗ Failed to pull base image"
        exit 1
    fi
else
    echo "  ✓ Base image found: jiayuanz3/memory:base"
fi

# Run evaluation
python3 -m swebench_memory.harness.run_evaluation \
    --dataset_name "$DATASET_FILE" \
    --predictions_path "$PREDICTIONS_FILE" \
    --run_id "$RUN_ID"

echo ""
echo "============================================================"
echo "Evaluation Complete!"
echo "============================================================"
echo "Results: ${RUN_ID}.json"
echo "Logs: logs/run_evaluation/${RUN_ID}/"

# Clean up temporary files
echo ""
echo "Cleaning up temporary files..."
rm -f "$DATASET_FILE" "$PREDICTIONS_FILE"
echo "✓ Removed: $DATASET_FILE"
echo "✓ Removed: $PREDICTIONS_FILE"
echo ""
