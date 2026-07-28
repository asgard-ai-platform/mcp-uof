"""http_web package — httpx + lxml UOF web backend (split from a single module)."""
from .session import (
    HttpSession,
    get_http_session,
    current_http_session,
    reset_http_session,
)
from .backend import HttpWebBackend
from .runtime import (
    EvidenceKind,
    HydratedPage,
    ReplayPolicy,
    ResponseEvidence,
    WebFormsRuntime,
)
from .details import DetailOperation, DetailWriteResult, PluginBlockResult
from .schema import FieldOption, FormField, FormSchema
from .field_codec import EncodeResult, FieldCodec
from .submission import SubmissionOperation
from .lifecycle import TaskLifecycleOperation
from .forms import FormsOperation
from .dialogs import DialogOperation
from .transport import HttpTransport

__all__ = [
    "HttpSession",
    "HttpWebBackend",
    "EvidenceKind",
    "HydratedPage",
    "ReplayPolicy",
    "ResponseEvidence",
    "WebFormsRuntime",
    "DetailOperation",
    "DetailWriteResult",
    "PluginBlockResult",
    "FieldOption",
    "FormField",
    "FormSchema",
    "EncodeResult",
    "FieldCodec",
    "SubmissionOperation",
    "TaskLifecycleOperation",
    "FormsOperation",
    "DialogOperation",
    "HttpTransport",
    "get_http_session",
    "current_http_session",
    "reset_http_session",
]
