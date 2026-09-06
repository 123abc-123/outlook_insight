class RadarError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class ModelError(RadarError):
    def __init__(self, message="模型调用失败或返回结构不符合要求"):
        super().__init__("model_error", message, 502)
