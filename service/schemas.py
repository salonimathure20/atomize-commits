"""Request and response schemas.

Under Flask, validating a request meant thirty lines of hand-written type
checks in the endpoint. Declaring the shape here does the same job, and it
also generates the OpenAPI document that powers the interactive docs at /docs.

Response models are declared too, which is less obviously useful but worth it:
FastAPI filters the outgoing payload down to exactly these fields, so adding
something internal to a dataclass can never accidentally leak into the API.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",  # a typo'd field is a mistake worth reporting, not ignoring
        json_schema_extra={
            "examples": [
                {
                    "diff": "diff --git a/app.py b/app.py\n@@ -1,2 +1,4 @@\n...",
                    "files": {"app.py": "def hello():\n    return 1\n"},
                    "messages": True,
                }
            ]
        },
    )

    diff: str = Field(
        ...,
        min_length=1,
        description="Output of `git diff`.",
    )
    files: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Current contents of the changed files, keyed by path. Optional: "
            "without it symbol detection is coarser but the analysis still runs."
        ),
    )
    co_change: list[tuple[str, str, float]] = Field(
        default_factory=list,
        description=(
            "Pairs of paths that historically change together, with a strength "
            "between 0 and 1. Mined from the caller's `git log`."
        ),
    )
    messages: bool = Field(
        default=True,
        description="Whether to ask a language model to write commit messages.",
    )

    @field_validator("diff")
    @classmethod
    def diff_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("diff must not be blank")
        return value

    def co_change_map(self) -> dict[tuple[str, str], float]:
        """Convert to the sorted-tuple keys the engine expects."""
        return {
            tuple(sorted((a, b))): strength
            for a, b, strength in self.co_change
        }


class CommitMessageOut(BaseModel):
    header: str = Field(description="First line of the commit message.")
    full: str = Field(description="Complete message, including any body.")
    type: str = Field(description="Conventional Commits type, e.g. feat or fix.")
    scope: str | None = None
    subject: str
    body: str | None = None
    generated_by: str = Field(
        description="Either 'model' or 'heuristic', depending on what produced it."
    )


class HunkOut(BaseModel):
    id: str
    path: str
    symbol: str | None = Field(
        default=None, description="Enclosing function or class, when known."
    )
    start_line: int
    additions: int
    deletions: int


class CommitOut(BaseModel):
    content_key: list[str] = Field(
        description=(
            "The changed lines themselves. Hunk ids shift once an earlier "
            "commit lands; these do not, so a saved plan stays valid."
        )
    )
    message: CommitMessageOut
    files: list[str]
    additions: int
    deletions: int
    reasons: list[str] = Field(
        default_factory=list,
        description="Why these changes were grouped together.",
    )
    hunks: list[HunkOut]


class AnalyzeResponse(BaseModel):
    commits: list[CommitOut]
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal notes, such as a commit that may not build alone.",
    )
    unsplittable: list[str] = Field(
        default_factory=list,
        description="Files that must travel whole, such as binaries and renames.",
    )


class ModelStatus(BaseModel):
    configured: bool
    provider: str | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    status: str
    version: str


class ReadyResponse(BaseModel):
    status: str
    version: str
    model: ModelStatus


class ErrorResponse(BaseModel):
    error: str
