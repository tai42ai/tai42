"""A dependency-only fixture library, published at two versions.

The mu and nu fixture plugins pin it with ranges that agree at one plugin version
and conflict at another, so a test proves the plugin layer resolves every
in-process plugin together: one copy for a compatible pair, a refused install for
a conflicting one."""
