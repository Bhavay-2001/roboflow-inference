import numpy as np
import pytest
import supervision as sv
from pydantic import ValidationError

from inference.core.workflows.core_steps.visualizations.pixelate import (
    PixelateManifest,
    PixelateVisualizationBlock,
)

from inference.core.workflows.entities.base import (
    WorkflowImageData,
    ImageParentMetadata,
)


@pytest.mark.parametrize("images_field_alias", ["images", "image"])
def test_pixelate_validation_when_valid_manifest_is_given(images_field_alias: str) -> None:
    # given
    data = {
        "type": "PixelateVisualization",
        "name": "pixelate1",
        "predictions": "$steps.od_model.predictions",
        images_field_alias: "$inputs.image",
        "pixel_size": 10
    }

    # when
    result = PixelateManifest.model_validate(data)

    # then
    assert result == PixelateManifest(
        type="PixelateVisualization",
        name="pixelate1",
        images="$inputs.image",
        predictions="$steps.od_model.predictions",
        pixel_size=10
    )


def test_pixelate_validation_when_invalid_image_is_given() -> None:
    # given
    data = {
        "type": "PixelateVisualization",
        "name": "pixelate1",
        "images": "invalid",
        "predictions": "$steps.od_model.predictions",
        "pixel_size": 10
    }

    # when
    with pytest.raises(ValidationError):
        _ = PixelateManifest.model_validate(data)


@pytest.mark.asyncio
async def test_pixelate_visualization_block() -> None:
    # given
    block = PixelateVisualizationBlock()

    start_image = np.random.randint(0, 255, (1000, 1000, 3), dtype=np.uint8)
    output = await block.run(
        image=WorkflowImageData(
            parent_metadata=ImageParentMetadata(parent_id="some"),
            numpy_image=start_image,
        ),
        predictions=sv.Detections(
            xyxy=np.array(
                [[0, 0, 20, 20], [80, 80, 120, 120], [450, 450, 550, 550]], dtype=np.float64
            ),
            class_id=np.array([1, 1, 1]),
        ),
        copy_image=True,
        pixel_size=10,
    )

    assert output is not None
    assert "image" in output
    assert hasattr(output.get("image"), "numpy_image")
    
    # dimensions of output match input
    assert output.get("image").numpy_image.shape == (1000, 1000, 3)
    # check if the image is modified
    assert not np.array_equal(output.get("image").numpy_image, start_image)
