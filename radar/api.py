from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from .config import load_config
from .decompose import decompose
from .errors import RadarError
from .markdown_store import MarkdownVersionStore
from .markdown_update import update_markdown_report
from .model import model_from_config
from .schemas import (DecomposeRequest, DecomposeResult, MarkdownUpdateRequest,
                      MarkdownUpdateResult)
from .store import Store


def create_app(model=None, store=None, report_root=None, history_root=None):
    config = load_config() if model is None or store is None else None
    app = FastAPI(title="认知雷达", version="0.1.0",
                  description="两个业务接口：话题拆分与证据支持的报告局部更新。")
    app.state.model = model if model is not None else model_from_config(config)
    app.state.store = store if store is not None else Store(config.app.database_path)
    report_settings = config.reports if config is not None else None
    app.state.markdown_versions = MarkdownVersionStore(
        app.state.store,
        report_root or (report_settings.root_dir if report_settings else "reports"),
        history_root or (report_settings.history_dir if report_settings else "data/report_versions"),
    )

    @app.exception_handler(RadarError)
    async def radar_error_handler(request, exc):
        return JSONResponse(status_code=exc.http_status,
                            content={"error": {"code": exc.code, "message": exc.message}})

    @app.post("/decompose", response_model=DecomposeResult)
    def decompose_endpoint(request: DecomposeRequest, response: Response):
        response.headers["X-Radar-Model-Mode"] = app.state.model.mode
        return decompose(request, app.state.model)

    @app.post("/update", response_model=MarkdownUpdateResult)
    def update_endpoint(request: MarkdownUpdateRequest, response: Response):
        result = update_markdown_report(request, app.state.model, app.state.markdown_versions)
        response.headers["X-Radar-Model-Mode"] = app.state.model.mode
        response.status_code = (409 if result.status == "version_conflict" else 422
                                if result.status not in {"updated", "preview_ready", "no_change"} else 200)
        return result

    return app
