import argparse
import asyncio
import tempfile
from pathlib import Path
from typing import Literal

import yaml
from langchain_core.messages import AIMessage
from pydantic import Field

from evals.fakes import ScriptedEventAgent, model_end, tool_start
from evals.models import AgentRunResult, CaseEvaluation, EvalCase, StrictModel, SuiteReport
from evals.loader import load_all_cases
from evals.runner import evaluate_case, make_suite_report, write_report


DEFAULT_FIXTURE_PATH = Path(__file__).with_name("fixtures") / "offline_traces.yaml"


class OfflineFixture(StrictModel):
    case_id: str
    result: AgentRunResult


class OfflineFixtureSuite(StrictModel):
    version: Literal[1]
    description: str = Field(min_length=1)
    fixtures: list[OfflineFixture] = Field(min_length=1)


def load_offline_fixtures(
    path: str | Path = DEFAULT_FIXTURE_PATH,
) -> dict[str, OfflineFixture]:
    with Path(path).open("r", encoding="utf-8") as file:
        suite = OfflineFixtureSuite.model_validate(yaml.safe_load(file))
    fixtures = {fixture.case_id: fixture for fixture in suite.fixtures}
    if len(fixtures) != len(suite.fixtures):
        raise ValueError("离线轨迹中存在重复的 case_id")
    return fixtures


def validate_fixture_coverage(
    cases: list[EvalCase],
    fixtures: dict[str, OfflineFixture],
) -> None:
    case_ids = {case.id for case in cases}
    fixture_ids = set(fixtures)
    missing = sorted(case_ids - fixture_ids)
    unknown = sorted(fixture_ids - case_ids)
    if missing or unknown:
        raise ValueError(f"离线轨迹与数据集不一致: missing={missing}, unknown={unknown}")


class OfflineScenarioAgent(ScriptedEventAgent):
    """Replay a deterministic Agent trace while creating declared fake outputs."""

    def __init__(self, fixture: OfflineFixture, session_dir: Path) -> None:
        super().__init__([])
        self.fixture = fixture
        self.session_dir = session_dir

    async def astream_events(self, input, config=None, *, version="v2"):
        self.calls.append((input, config, version))
        for subagent in self.fixture.result.subagent_calls:
            yield tool_start("task", {"subagent_type": subagent, "description": input})
        for call in self.fixture.result.tool_calls:
            yield tool_start(call.name, call.arguments)
        for declared_path in self.fixture.result.generated_files:
            target = self.session_dir / declared_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("offline evaluation artifact", encoding="utf-8")
        if self.fixture.result.answer:
            yield model_end(AIMessage(content=self.fixture.result.answer))
        if self.fixture.result.error:
            yield {
                "event": "on_chain_error",
                "data": {"error": self.fixture.result.error},
            }


async def evaluate_offline_suite(
    cases: list[EvalCase],
    workspace: str | Path,
    fixtures: dict[str, OfflineFixture] | None = None,
) -> SuiteReport:
    selected_fixtures = fixtures or load_offline_fixtures()
    validate_fixture_coverage(cases, selected_fixtures)
    workspace_path = Path(workspace)
    evaluations: list[CaseEvaluation] = []
    for case in cases:
        session_dir = workspace_path / case.id
        session_dir.mkdir(parents=True, exist_ok=True)
        evaluations.append(
            await evaluate_case(
                case,
                OfflineScenarioAgent(selected_fixtures[case.id], session_dir),
                session_dir=session_dir,
            )
        )
    return make_suite_report(evaluations)


async def run_offline_evaluations(
    workspace: str | Path,
    *,
    report_path: str | Path | None = None,
) -> SuiteReport:
    report = await evaluate_offline_suite(load_all_cases(), workspace)
    if report_path is not None:
        write_report(report, report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="运行离线 Agent 参考轨迹评测")
    parser.add_argument(
        "--report",
        default="eval-results/offline-report.json",
        help="JSON 报告输出路径",
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="deep-search-evals-") as workspace:
        report = asyncio.run(
            run_offline_evaluations(workspace, report_path=args.report)
        )
    print(
        f"Offline evaluations: {report.passed}/{report.total} passed, "
        f"mean score={report.mean_score:.2%}, report={args.report}"
    )
    return 0 if report.passed == report.total else 1


if __name__ == "__main__":
    raise SystemExit(main())
