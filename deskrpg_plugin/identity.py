from aiohttp import web

SOUL_FILENAME = "SOUL.md"


def _stub(name):
    async def handler(request):
        return web.json_response({"error": f"{name} not implemented"}, status=501)

    return handler


def get_handler(api):
    return _stub("get_identity")


def put_handler(api):
    return _stub("put_identity")
