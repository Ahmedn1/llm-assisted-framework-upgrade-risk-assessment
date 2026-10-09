"""Explicit native decision smoke call to a TypeSafe-compatible endpoint."""
import argparse

from inference import ChoiceQuestion, ModelConfig, NoulQuestion, call_decision_model


def main():
    parser = argparse.ArgumentParser(description="Call JEV_BASE_URL using JEV_MODEL and JEV_API_KEY")
    parser.add_argument("--text", default="The page says: Release archive, maintained by this project; links to versions 4.0, 4.1, and 4.2.")
    args = parser.parse_args()
    result = call_decision_model(
        state={"source_id": "s1", "excerpt": args.text},
        questions={
            "source_kind": ChoiceQuestion(
                instructions="Classify this page using only its supplied excerpt.",
                criteria={"archive": "Project release archive", "mirror": "Source code mirror", "unknown": "Not established by the excerpt"},
            ),
            "lists_versions": NoulQuestion(instructions="Does the excerpt list software release versions?"),
        },
        config=ModelConfig.from_env("JEV"),
    )
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
