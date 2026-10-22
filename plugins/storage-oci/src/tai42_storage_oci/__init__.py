"""OCI Object Storage ``Storage`` backend for the TAI ecosystem.

Importing this package registers ``OciStorage`` as the active storage provider
via an import side-effect.
"""

from tai42_storage_oci.storage import OciStorage

__all__ = ["OciStorage"]
