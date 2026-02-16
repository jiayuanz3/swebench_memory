#!/usr/bin/env python3
"""
Combine multiple instance and prediction files for batch evaluation

Usage:
    python combine_instances.py --instances cases/ --predictions predictions/
"""

import argparse
import json
from pathlib import Path


def load_all_json_files(directory, verbose=False):
    """Load and combine all JSON files from a directory"""
    dir_path = Path(directory)
    all_data = []

    if not dir_path.exists():
        print(f"⚠ Directory not found: {directory}")
        return all_data

    json_files = sorted(dir_path.glob("*.json"))

    for json_file in json_files:
        try:
            with open(json_file) as f:
                data = json.load(f)

                # Handle different formats
                if isinstance(data, list):
                    all_data.extend(data)
                elif isinstance(data, dict):
                    # Handle dict format: {"instance_id": {...}}
                    # Check if it's a wrapped prediction format
                    if any('instance_id' in v and 'model_patch' in v for v in data.values() if isinstance(v, dict)):
                        # This is a wrapped prediction: {"id": {"instance_id": "id", ...}}
                        for key, value in data.items():
                            if isinstance(value, dict):
                                all_data.append(value)
                    else:
                        # Regular dict, just append
                        all_data.append(data)
                else:
                    all_data.append(data)

                if verbose:
                    print(f"  ✓ Loaded: {json_file.name}")
        except Exception as e:
            print(f"  ✗ Error loading {json_file.name}: {e}")

    return all_data


def main():
    parser = argparse.ArgumentParser(description="Combine SWE-bench instances and predictions")
    parser.add_argument("--instances", default="cases", help="Directory containing instance files")
    parser.add_argument("--predictions", default="predictions", help="Directory containing prediction files")
    parser.add_argument("--dataset-output", default="batch_dataset.json", help="Output dataset file")
    parser.add_argument("--predictions-output", default="batch_predictions.json", help="Output predictions file")

    args = parser.parse_args()

    # Load all prediction files first (these determine which instances to include)
    print(f"Loading predictions from: {args.predictions}")
    predictions = load_all_json_files(args.predictions, verbose=False)
    print(f"✓ Loaded {len(predictions)} prediction(s)")

    # Get instance IDs from predictions
    prediction_instance_ids = set()
    for pred in predictions:
        if 'instance_id' in pred:
            prediction_instance_ids.add(pred['instance_id'])

    # Load all instances (silently)
    all_instances = load_all_json_files(args.instances, verbose=False)

    # Filter instances to only include those with predictions
    filtered_instances = []
    for instance in all_instances:
        if 'instance_id' in instance and instance['instance_id'] in prediction_instance_ids:
            filtered_instances.append(instance)
            print(f"✓ Matched: {instance['instance_id']}")

    # Check for predictions without matching instances
    instance_ids = {inst['instance_id'] for inst in filtered_instances}
    for pred_id in prediction_instance_ids:
        if pred_id not in instance_ids:
            print(f"⚠ No instance file found for prediction: {pred_id}")

    # Write combined files (silently)
    with open(args.dataset_output, 'w') as f:
        json.dump(filtered_instances, f, indent=2)

    with open(args.predictions_output, 'w') as f:
        json.dump(predictions, f, indent=2)


if __name__ == "__main__":
    main()
