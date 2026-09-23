"""Runtime-native builtin tools shipped with the OS.

Each module registers its tool through the ``tai42_app`` handle
(``@tai42_app.tools.tool``) exactly as an external plugin would; there is no
default module list, so a deployment opts each module in by naming it in a
manifest ``tools[].module`` entry. Most are runtime-native — with no HTTP route
equivalent by nature — and the ``doors`` tools are the in-process face of a
platform door that ALSO serves external callers over HTTP, a DISTINCT surface:
the deployment self-calling its own door under the run's identity, with no HTTP
request and no key. The tools are ``file_loader`` (load a file from a url or
storage resource id), ``interactions`` (the ``ask`` human-in-the-loop tool),
``get_pairing_code`` (mint a single-use pair code for a channel conversation),
``set_conversation_mode`` (flip the current conversation between ``agent`` and
``manual`` control from inside an agent turn), and ``doors``
(``send_conversation_message`` / ``send_conversation_event`` — self-call the
message/event conversation doors under the run's identity).

Management capabilities are not builtin tool modules: they live in the
operations layer and project onto the MCP tool surface directly from the
operations registry (gated by the manifest ``api_tools`` block), so there is no
hand-written management-tool module to opt in.
"""
