from typing import List, Literal, Optional, Type, Union

import supervision as sv
from pydantic import ConfigDict, Field

from inference.core.workflows.core_steps.visualizations.base import OUTPUT_IMAGE_KEY
from inference.core.workflows.core_steps.visualizations.base_colorable import (
    ColorableVisualizationBlock,
    ColorableVisualizationManifest,
)
from inference.core.workflows.entities.base import WorkflowImageData
from inference.core.workflows.entities.types import (
    FLOAT_ZERO_TO_ONE_KIND,
    INTEGER_KIND,
    FloatZeroToOne,
    WorkflowParameterSelector,
)
from inference.core.workflows.prototypes.block import BlockResult, WorkflowBlockManifest

TYPE: str = "BoundingBoxVisualization"
SHORT_DESCRIPTION = "Draws a box around detected objects in an image."
LONG_DESCRIPTION = """
The `BoundingBoxVisualization` block draws a box around detected
objects in an image using Supervision's `sv.RoundBoxAnnotator`.
"""


class BoundingBoxManifest(ColorableVisualizationManifest):
    type: Literal[f"{TYPE}"]
    model_config = ConfigDict(
        json_schema_extra={
            "short_description": SHORT_DESCRIPTION,
            "long_description": LONG_DESCRIPTION,
            "license": "Apache-2.0",
            "block_type": "visualization",
        }
    )

    thickness: Union[int, WorkflowParameterSelector(kind=[INTEGER_KIND])] = Field(  # type: ignore
        description="Thickness of the bounding box in pixels.",
        default=2,
        examples=[2, "$inputs.thickness"],
    )

    roundness: Union[FloatZeroToOne, WorkflowParameterSelector(kind=[FLOAT_ZERO_TO_ONE_KIND])] = Field(  # type: ignore
        description="Roundness of the corners of the bounding box.",
        default=0.0,
        examples=[0.0, "$inputs.roundness"],
    )


class BoundingBoxVisualizationBlock(ColorableVisualizationBlock):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.annotatorCache = {}

    @classmethod
    def get_manifest(cls) -> Type[WorkflowBlockManifest]:
        return BoundingBoxManifest

    def getAnnotator(
        self,
        color_palette: str,
        palette_size: int,
        custom_colors: List[str],
        color_axis: str,
        thickness: int,
        roundness: float,
    ) -> sv.annotators.base.BaseAnnotator:
        key = "_".join(
            map(str, [color_palette, palette_size, color_axis, thickness, roundness])
        )

        if key not in self.annotatorCache:
            palette = self.getPalette(color_palette, palette_size, custom_colors)

            if roundness == 0:
                self.annotatorCache[key] = sv.BoxAnnotator(
                    color=palette,
                    color_lookup=getattr(sv.ColorLookup, color_axis),
                    thickness=thickness,
                )
            else:
                self.annotatorCache[key] = sv.RoundBoxAnnotator(
                    color=palette,
                    color_lookup=getattr(sv.ColorLookup, color_axis),
                    thickness=thickness,
                    roundness=roundness,
                )
        return self.annotatorCache[key]

    async def run(
        self,
        image: WorkflowImageData,
        predictions: sv.Detections,
        copy_image: bool,
        color_palette: Optional[str],
        palette_size: Optional[int],
        custom_colors: Optional[List[str]],
        color_axis: Optional[str],
        thickness: Optional[int],
        roundness: Optional[float],
    ) -> BlockResult:
        annotator = self.getAnnotator(
            color_palette,
            palette_size,
            custom_colors,
            color_axis,
            thickness,
            roundness,
        )

        annotated_image = annotator.annotate(
            scene=image.numpy_image.copy() if copy_image else image.numpy_image,
            detections=predictions,
        )

        output = WorkflowImageData(
            parent_metadata=image.parent_metadata,
            workflow_root_ancestor_metadata=image.workflow_root_ancestor_metadata,
            numpy_image=annotated_image,
        )

        return {OUTPUT_IMAGE_KEY: output}
