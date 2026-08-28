"""Helper: import the REAL, unmodified ~/.claude-telegram-bridge/jarvis-ask/claude_ask.py
as a live Python module, by stubbing out its Hikka/herokutl framework
dependencies (which aren't installed in this sandbox and aren't safe/possible
to install here). This does NOT copy or modify claude_ask.py -- it loads the
actual file from disk via importlib, with fake `loader`/`utils`/`_internal`
(relative-import parents) and a fake `herokutl` package pre-registered in
sys.modules so the real module's top-level imports succeed.

Only decorators/framework glue are stubbed (identity decorators, dummy TL
classes). Every method body below the class statement is the real, verbatim
shipped code -- nothing here changes what claude_ask.py actually does.
"""
import importlib.util
import os
import sys
import types

REAL_CLAUDE_ASK_PATH = os.path.expanduser(
    "~/.claude-telegram-bridge/jarvis-ask/claude_ask.py"
)


def _install_fake_herokutl():
    if "herokutl" in sys.modules:
        return

    herokutl = types.ModuleType("herokutl")
    herokutl.__path__ = []
    tl = types.ModuleType("herokutl.tl")
    tl.__path__ = []
    functions = types.ModuleType("herokutl.tl.functions")
    functions.__path__ = []

    def _kwargs_init(self, *a, **kw):
        self.args = a
        self.kwargs = kw

    def _make_stub(name):
        return type(name, (), {"__init__": _kwargs_init})

    channels = types.ModuleType("herokutl.tl.functions.channels")
    for name in ("ToggleForumRequest", "InviteToChannelRequest", "GetParticipantRequest"):
        setattr(channels, name, _make_stub(name))

    messages = types.ModuleType("herokutl.tl.functions.messages")
    for name in ("ExportChatInviteRequest", "EditForumTopicRequest"):
        setattr(messages, name, _make_stub(name))

    contacts = types.ModuleType("herokutl.tl.functions.contacts")
    for name in ("AddContactRequest", "DeleteContactsRequest", "BlockRequest", "UnblockRequest"):
        setattr(contacts, name, _make_stub(name))

    types_mod = types.ModuleType("herokutl.tl.types")
    for name in ("MessageEntityUrl", "MessageEntityTextUrl", "Channel", "ChannelParticipantsAdmins"):
        setattr(types_mod, name, _make_stub(name))

    errors = types.ModuleType("herokutl.errors")
    for name in ("UserPrivacyRestrictedError", "UserNotParticipantError"):
        setattr(errors, name, type(name, (Exception,), {}))

    sys.modules["herokutl"] = herokutl
    sys.modules["herokutl.tl"] = tl
    sys.modules["herokutl.tl.functions"] = functions
    sys.modules["herokutl.tl.functions.channels"] = channels
    sys.modules["herokutl.tl.functions.messages"] = messages
    sys.modules["herokutl.tl.functions.contacts"] = contacts
    sys.modules["herokutl.tl.types"] = types_mod
    sys.modules["herokutl.errors"] = errors


def _install_fake_hikka_package():
    """Builds a synthetic `fakepkg.loaded_modules.claude_ask` location so the
    real file's `from .. import loader, utils` / `from .._internal import
    fw_protect` relative imports resolve to our stubs."""
    if "fakepkg" in sys.modules:
        return

    fakepkg = types.ModuleType("fakepkg")
    fakepkg.__path__ = []

    loader_mod = types.ModuleType("fakepkg.loader")

    class Module:
        strings = {}

        def __init__(self, *a, **kw):
            pass

    def _identity_decorator_factory(*a, **kw):
        def deco(f):
            return f
        return deco

    loader_mod.Module = Module
    loader_mod.tds = lambda cls: cls  # class decorator, identity
    loader_mod.command = _identity_decorator_factory
    loader_mod.loop = _identity_decorator_factory
    loader_mod.watcher = _identity_decorator_factory

    class _Validators:
        pass

    loader_mod.validators = _Validators()

    utils_mod = types.ModuleType("fakepkg.utils")

    async def _stub_async(*a, **kw):
        return None

    utils_mod.asset_forum_topic = _stub_async
    utils_mod.get_args_raw = lambda message: (getattr(message, "raw_text", "") or "").split(maxsplit=1)[-1] if getattr(message, "raw_text", "") else ""
    utils_mod.get_args = lambda message: []
    utils_mod.answer = _stub_async

    internal_mod = types.ModuleType("fakepkg._internal")
    internal_mod.fw_protect = lambda *a, **kw: None

    loaded_modules_pkg = types.ModuleType("fakepkg.loaded_modules")
    loaded_modules_pkg.__path__ = []

    sys.modules["fakepkg"] = fakepkg
    sys.modules["fakepkg.loader"] = loader_mod
    sys.modules["fakepkg.utils"] = utils_mod
    sys.modules["fakepkg._internal"] = internal_mod
    sys.modules["fakepkg.loaded_modules"] = loaded_modules_pkg
    fakepkg.loader = loader_mod
    fakepkg.utils = utils_mod
    fakepkg._internal = internal_mod


def load_real_claude_ask_module():
    """Returns the real claude_ask module object (its ClaudeAsk class is the
    actual shipped implementation, unmodified) with framework deps stubbed."""
    _install_fake_herokutl()
    _install_fake_hikka_package()

    mod_name = "fakepkg.loaded_modules.claude_ask"
    if mod_name in sys.modules:
        return sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location(mod_name, REAL_CLAUDE_ASK_PATH)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "fakepkg.loaded_modules"
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def make_bare_instance(claude_ask_module):
    """A ClaudeAsk instance with __init__ bypassed (loader.Module.__init__
    does nothing useful for us anyway) -- fine for exercising any method
    that doesn't touch self._client/self.db, which is exactly the code path
    under test."""
    cls = claude_ask_module.ClaudeAsk
    return cls.__new__(cls)
