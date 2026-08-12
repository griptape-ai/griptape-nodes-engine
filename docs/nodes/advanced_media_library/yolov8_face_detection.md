# YOLOv8 Face Detection

!!! warning "You need to perform setup steps to use Hugging Face models"

    [This guide](../../guides/integrations/hugging_face.md) will walk you through setting up a Hugging Face account, creating an access token, and installing the required models to make this node fully functional.

## What is it?

YOLOv8 Face Detection is a computer vision node that detects human faces in images using the YOLOv8 (You Only Look Once version 8) object detection model from 🤗 Hugging Face. The node processes images and returns bounding box coordinates for each detected face along with confidence scores.

The implementation uses the `arnabdhar/YOLOv8-Face-Detection` model, which is specifically trained for face detection tasks and provides fast, accurate results suitable for real-time applications.

## When would I use it?

Use this node when you need to:

- Detect faces in images for face recognition pipelines
- Crop or extract face regions from photos
- Count the number of people in an image
- Filter images based on face presence or absence
- Create face-focused compositions or effects
- Process images for privacy protection (face blurring/masking)
- Build automated photo organization systems
- Implement face-based access control systems

## How to use it

### Basic Setup

1. **Add the Node**:

    - Add a "YOLOv8 Face Detection" node to your workflow
    - The node will automatically download the model on first use (requires Hugging Face access)

1. **Connect Input**:

    - Connect an `ImageArtifact` or `ImageUrlArtifact` to the `input_image` parameter
    - This can come from file loaders, image generation nodes, or other image processing nodes

1. **Configure Parameters**:

    - Set the `confidence_threshold` (0.0-1.0) to filter detections
    - Optionally set `dilation` (0-100%) to expand the detected bounding boxes

1. **Run Detection**:

    - Execute the node to detect faces in the input image
    - The `detected_faces` output will contain a list of face detections

### Parameters

#### Input Parameters

- **input_image** (required)

    - Type: `ImageArtifact` or `ImageUrlArtifact`
    - The image to analyze for face detection

- **confidence_threshold**

    - Type: `float` (0.0-1.0)
    - Default: `0.5`
    - Minimum confidence score for a detection to be included in the results
    - Higher values = fewer but more confident detections
    - Lower values = more detections but may include false positives

- **dilation**

    - Type: `float` (0.0-100.0)
    - Default: `0.0`
    - Percentage to expand bounding boxes while keeping them centered
    - Useful for including more context around detected faces
    - Example: `10.0` expands the box by 10% in all directions

#### Output Parameters

- **detected_faces**

    - Type: `list`
    - A list of detected faces, each containing:
        - `x`: Top-left x-coordinate of the bounding box
        - `y`: Top-left y-coordinate of the bounding box
        - `width`: Width of the bounding box
        - `height`: Height of the bounding box
        - `confidence`: Detection confidence score (0.0-1.0)

- **logs**

    - Type: `string`
    - Detailed logs of the detection process including:
        - Model loading status
        - Detection parameters
        - Number of faces detected

### Output Format

Each detected face is represented as a dictionary:

```json
{
  "x": 150,
  "y": 200,
  "width": 300,
  "height": 350,
  "confidence": 0.95
}
```

The bounding box coordinates are in pixels relative to the input image dimensions, with the origin (0,0) at the top-left corner.

### Example Workflows

#### Basic Face Detection

1. Load an image using "File to Bytes" or similar node
1. Connect to YOLOv8 Face Detection's `input_image`
1. Set `confidence_threshold` to `0.5` for balanced detection
1. The `detected_faces` output contains all face locations and confidence scores

#### Face Cropping Pipeline

1. Detect faces using YOLOv8 Face Detection
1. Set `dilation` to `10.0` to include some background around faces
1. Connect `detected_faces` to a "Crop Image" node
1. Extract individual face images for further processing

#### High-Confidence Detection Only

1. Set `confidence_threshold` to `0.8` or higher
1. This filters out uncertain detections
1. Ideal for applications requiring high precision

### Advanced Features

- **Automatic Model Caching**: Downloaded models are cached locally for faster subsequent runs
- **Boundary Clamping**: Dilated bounding boxes are automatically clamped to image boundaries
- **Centered Dilation**: Box expansion maintains the center point of the original detection
- **Batch-Compatible**: Process multiple images by connecting to loop structures

## Performance Considerations

- **First Run**: The initial execution downloads the model (~6MB), which may take a few moments
- **Subsequent Runs**: Cached models load almost instantly
- **Image Size**: Larger images take longer to process but may detect more distant faces
- **Detection Speed**: YOLOv8 is optimized for real-time performance, typically processing images in milliseconds
- **Memory Usage**: Model requires ~50MB of RAM when loaded

## Common Issues

- **Missing API Key**: Ensure the Hugging Face API token is set as `HF_TOKEN`; instructions for that are in [this guide](../../guides/integrations/hugging_face.md)
- **Model Not Found**: If you see "model not available" warnings, click the provided link to open the Model Manager and download the model
- **No Faces Detected**: Try lowering the `confidence_threshold` if you expect faces but none are detected
- **Too Many False Positives**: Increase the `confidence_threshold` to filter out low-confidence detections
- **Bounding Box Issues**: If boxes seem too tight, increase the `dilation` parameter to add padding

## Technical Details

- **Model**: YOLOv8 Face Detection from Hugging Face (`arnabdhar/YOLOv8-Face-Detection`)
- **Architecture**: YOLOv8 object detection framework specialized for face detection
- **Dependencies**: `ultralytics>=8.0.0`, `supervision>=0.20.0`
- **Output Format**: Standard bounding box format (x, y, width, height) with confidence scores
- **Processing**: Single-pass detection, optimized for speed and accuracy
