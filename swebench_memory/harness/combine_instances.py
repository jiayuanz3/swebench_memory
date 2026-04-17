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

    # Get instance IDs from predictions (preserve originals, also build lowercase lookup)
    prediction_instance_ids = {}  # lowercase -> original
    for pred in predictions:
        if 'instance_id' in pred:
            prediction_instance_ids[pred['instance_id'].lower()] = pred['instance_id']

    # Load all instances (silently)
    all_instances = load_all_json_files(args.instances, verbose=False)

    # Filter instances to only include those with predictions (case-insensitive match)
    # Also normalize instance_id to match the prediction's casing
    filtered_instances = []
    matched_pred_ids_lower = set()
    for instance in all_instances:
        if 'instance_id' in instance:
            lower_id = instance['instance_id'].lower()
            if lower_id in prediction_instance_ids:
                # Normalize instance_id to match prediction casing
                canonical_id = prediction_instance_ids[lower_id]
                instance = dict(instance)
                instance['instance_id'] = canonical_id
                filtered_instances.append(instance)
                matched_pred_ids_lower.add(lower_id)
                print(f"✓ Matched: {canonical_id}")

    # Check for predictions without matching instances
    for lower_id, orig_id in prediction_instance_ids.items():
        if lower_id not in matched_pred_ids_lower:
            print(f"⚠ No instance file found for prediction: {orig_id}")

    # Write combined files (silently)
    with open(args.dataset_output, 'w') as f:
        json.dump(filtered_instances, f, indent=2)

    with open(args.predictions_output, 'w') as f:
        json.dump(predictions, f, indent=2)


if __name__ == "__main__":
    main()
