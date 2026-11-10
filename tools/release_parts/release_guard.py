"""在客户端无源码发布方案完成前，阻止生成或放行安装包。"""


def require_source_free_release() -> None:
    # 当前 WebView 直接依赖项目 JavaScript 源码；白名单不能授权附带开发源码。
    raise RuntimeError("客户端发布已暂停：运行载荷仍包含项目开发源码")
