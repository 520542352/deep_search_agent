from pathlib import Path

import yaml

from evals.models import EvalCase, EvalSuite


DEFAULT_DATASET_DIR = Path(__file__).with_name("datasets")


def load_suite(path: str | Path) -> EvalSuite:
    """Load and strictly validate one versioned YAML evaluation suite."""
    dataset_path = Path(path)
    with dataset_path.open("r", encoding="utf-8") as file:
        payload = yaml.safe_load(file)
    return EvalSuite.model_validate(payload)


def load_all_cases(
    dataset_dir: str | Path = DEFAULT_DATASET_DIR,
    *,
    enabled_only: bool = True,
) -> list[EvalCase]:
    """Load all suites in deterministic order and reject duplicate case IDs."""
    cases: list[EvalCase] = []
    seen_ids: set[str] = set()
    for path in sorted(Path(dataset_dir).glob("*.yaml")):
        suite = load_suite(path)
        for case in suite.cases:
            if case.id in seen_ids:
                raise ValueError(f"重复的评测用例 ID: {case.id}")
            seen_ids.add(case.id)
            if not enabled_only or case.enabled:
                cases.append(case)
    if not cases:
        raise ValueError(f"评测数据目录中没有可用用例: {dataset_dir}")
    return cases
