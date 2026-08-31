from aiohttp import web


def _stub(name):
    async def handler(request):
        return web.json_response({"error": f"{name} not implemented"}, status=501)

    return handler


def get_handler(api):
    return _stub("get_config")


def put_handler(api):
    return _stub("put_config")
