"""不依赖业务类型或界面的公共错误。"""


class FfmpegError(Exception):
    """工具定位、能力检测或命令执行的可预期错误。"""
