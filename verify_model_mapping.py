"""Compare class mappings for Keras classifiers against labeled cucumber crops."""
import argparse
from pathlib import Path

from backend.model_classifier import (
    MODEL_PATH,
    compare_mapping_metrics,
    load_model,
    predict_model_output,
)


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def labeled_images(directory: Path, is_good: bool):
    return [
        (path, is_good)
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    ]


def verify_mapping(good_dir: Path, bad_dir: Path, model_path: Path = MODEL_PATH):
    from PIL import Image

    samples = labeled_images(good_dir, True) + labeled_images(bad_dir, False)
    if not samples:
        raise ValueError("no supported sample images found in the supplied directories")

    model = load_model(str(model_path))
    scores = []
    for path, is_good in samples:
        with Image.open(path) as image:
            outputs, _, _ = predict_model_output(image.convert("RGB"), model)
        scores.append((outputs, is_good))

    return len(scores), compare_mapping_metrics(scores), model.input_shape, model.output_shape


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--good-dir", type=Path, required=True, help="Directory of confirmed-good cucumber crops")
    parser.add_argument("--bad-dir", type=Path, required=True, help="Directory of confirmed-bad cucumber crops")
    parser.add_argument("--model", type=Path, default=MODEL_PATH, help="Path to the .keras model")
    parser.add_argument(
        "--compare-model",
        type=Path,
        action="append",
        default=[],
        help="Additional .keras model to compare on the same samples; repeat as needed",
    )
    args = parser.parse_args(argv)

    model_paths = [args.model, *args.compare_model]
    for model_path in model_paths:
        try:
            total, metrics, input_shape, output_shape = verify_mapping(
                args.good_dir, args.bad_dir, model_path
            )
        except (FileNotFoundError, ValueError) as error:
            parser.error(f"{model_path}: {error}")

        print(f"\nModel: {model_path} (input={input_shape}, output={output_shape})")
        print(f"Labeled crops: {total}")
        if output_shape[-1] == 1:
            mapping_labels = {
                "default": "sigmoid >= 0.5 means Good",
                "flipped": "sigmoid < 0.5 means Good",
            }
        else:
            mapping_labels = {
                "default": "softmax index 1 means Good",
                "flipped": "softmax index 0 means Good",
            }
        for mapping in ("default", "flipped"):
            result = metrics[mapping]
            correct = result["correct"]
            good_total = result["good_total"]
            bad_total = result["bad_total"]
            print(
                f"{mapping.title()} ({mapping_labels[mapping]}): "
                f"{correct}/{total} ({correct / total:.1%}); "
                f"Good recall {result['good_correct']}/{good_total} "
                f"({result['good_correct'] / good_total:.1%}), "
                f"Bad recall {result['bad_correct']}/{bad_total} "
                f"({result['bad_correct'] / bad_total:.1%})"
            )
        if metrics["default"]["correct"] > metrics["flipped"]["correct"]:
            print(f"Mapping candidate: {mapping_labels['default']}")
        elif metrics["flipped"]["correct"] > metrics["default"]["correct"]:
            print(f"Mapping candidate: {mapping_labels['flipped']}")
        else:
            print("Mapping is tied on these samples; do not enable model alerts yet.")


if __name__ == "__main__":
    main()