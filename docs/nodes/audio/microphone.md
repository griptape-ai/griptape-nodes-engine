# Microphone

## What is it?

The Microphone node allows you to capture audio directly from your computer's microphone. It's a simple way to record audio input that can be used in your workflow.

## When would I use it?

Use this node when you want to:

- Record audio directly from your microphone
- Capture voice input for transcription
- Create audio recordings as part of your workflow
- Provide real-time audio input to other nodes

## How to use it

### Basic Setup

1. Add the Microphone node to your workflow
1. Click the microphone icon to start recording
1. Speak or provide audio input
1. Stop recording when finished

### Parameters

- **audio**: The captured audio output (AudioUrlArtifact)

### Outputs

- **audio**: The recorded audio that can be used by other nodes in your flow

## Example

A simple workflow to record and transcribe audio:

1. Add a Microphone node to your workflow
1. Record your audio input
1. Connect the "audio" output to a TranscribeAudio node
1. The transcription will be available in the TranscribeAudio node's output

## Important Notes

- The node requires microphone permissions in your browser
- Audio is captured in real-time
- The quality of the recording depends on your microphone and system settings
- Whatever format your browser records in, the capture is saved to a file and output as an AudioUrlArtifact

## Common Issues

- **No Microphone Access**: Ensure your browser has permission to access your microphone
- **Poor Audio Quality**: Check your microphone settings and system audio configuration
- **Recording Not Starting**: Make sure no other application is using the microphone
