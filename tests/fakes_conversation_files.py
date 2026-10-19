"""Advertise public attachment APIs for route/auth tests; runtime tests supply real behavior."""
def install_conversation_file_symbols(api):
    def unconfigured(*_args, **_kwargs):
        raise AssertionError('configure the attachment runtime explicitly for execution tests')
    for name in ('install_profile_terminal_scope', 'reset_terminal_scope', 'terminal_env',
                 'set_session_vars', 'clear_session_vars', 'to_agent_visible_cache_path',
                 'conversation_dispatch_tool'):
        setattr(api, name, unconfigured)
    for name in ('get_session', 'resolve_resume_session_id', 'get_compression_chain', 'get_compression_lineage'):
        setattr(api.SessionDB, name, unconfigured)
