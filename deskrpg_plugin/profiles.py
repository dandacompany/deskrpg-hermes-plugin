from aiohttp import web


def _stub(name):
    async def handler(request):
        return web.json_response({"error": f"{name} not implemented"}, status=501)

    return handler


def list_handler(api):
    return _stub("list_profiles")


def create_handler(api):
    return _stub("create_profile")


def delete_handler(api):
    return _stub("delete_profile")
