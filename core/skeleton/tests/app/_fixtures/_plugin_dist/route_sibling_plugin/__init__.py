"""A fixture distribution whose route lives in a SIBLING of its manifest leaf.

Models the channel-plugin shape (web/whatsapp/slack): the manifest-named leaf
registers by importing a sibling for its ``@custom_route`` side-effect, so a
leaf-only re-import leaves the sibling cached and its routes never re-fire.
"""
