"""Source registry. Add a source by writing a module here and listing it below."""

from __future__ import annotations

from swathcast.sources.asf import ASFGRD, ASFSLC
from swathcast.sources.base import Found, ReferenceCatalog, Source, SourceError
from swathcast.sources.cdse import CDSECatalog, CDSEProduct
from swathcast.sources.opera import OperaCSLC, OperaRTC
from swathcast.sources.pc import PCGRD, PCRTC

#: every phase-1 source, in display order
SOURCE_CLASSES: dict[str, type[Source]] = {
    cls.key: cls
    for cls in (
        # reference products (rows are created "seen" by the CDSE listing)
        type(
            "CDSEGRD",
            (CDSEProduct,),
            {
                "key": "cdse_grd",
                "label": "CDSE GRD",
                "product": "GRD",
                "reference_product": "GRD",
            },
        ),
        type(
            "CDSESLC",
            (CDSEProduct,),
            {
                "key": "cdse_slc",
                "label": "CDSE SLC",
                "product": "SLC",
                "reference_product": "SLC",
            },
        ),
        ASFGRD,
        ASFSLC,
        PCGRD,
        PCRTC,
        OperaRTC,
        OperaCSLC,
    )
}

__all__ = [
    "SOURCE_CLASSES",
    "CDSECatalog",
    "Found",
    "ReferenceCatalog",
    "Source",
    "SourceError",
]
