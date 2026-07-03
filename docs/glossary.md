# Griptape Nodes Glossary for Artists & Creators

## What is Griptape Nodes?

Griptape Nodes is a toolkit that enables artists and creators to build AI-powered projects without the need for deep technical expertise. You can think of Griptape Nodes as a set of building blocks that you can connect together to create art, generate images, process text, or even build other workflow-centric applications.

### Core Griptape Nodes Concepts

- **Workflow**: A document containing nodes, connections, and values. While technically a Workflow is also a Script, avoid calling them Scripts, so that the term Script can communicate a clearly different thing than the term Workflow. This is also what we call the saved file.

- **Workflow Editor**: The workspace where nodes are added, connected, and configured.

- **Flow**: A collection of connected nodes that form a functional unit. A Workflow can contain 0-n Flows.

- **Sub-Flow**: A contained set of nodes that executes within a Loop, Branch, Reference (and sometimes Groups). This refers to any logically distinct, internally-contained portion of a flow that functions as a cohesive unit.

- **Reference**: A link to another Flow, embedding it as a Sub-Flow within the current context.

- **Group**: A visual clustering of nodes. A Group may act as a Sub-Flow if it is executed as a unit, but grouping alone does not imply execution and may simply be a subjectively related set of nodes.

- **Script**: A Python script that runs code. This term should be avoided when describing a Workflow; instead, script refers to tools, macros, or flow-building aids.

- **Libraries**: Collections of node definitions and/or scripts that extend functionality. See [Libraries](libraries.md) for installing and managing them.

- **Node**: A single piece of the puzzle in your workflow. Nodes are like LEGO blocks that you can connect to create something bigger. Each node does one specific thing (like generating an image or processing text). There are **types** of nodes:

    - **DataNode**: A node that handles information — it can transform data from one form to another, or simply hold data you set.
    - **ControlNode**: A node that will "do some work". Control Nodes usually take longer to run than DataNodes because, they're usually doing more work.
    - **DriverNode**: A node that connects your project to outside services (like image generators or search engines).

- **Tool**: A ready-to-use component that performs a specific function. Tools are like brushes in your digital toolkit — each designed for a specific purpose.

    - **CalculatorTool**: Does math calculations for you.
    - **DateTimeTool**: Works with dates and times.
    - **FileManagerTool**: Helps manage files (saving, loading, etc.).
    - **WebScraperTool**: Collects information from websites.
    - **PromptSummaryTool**: Creates summaries of longer text.

- **Driver**: The connector between your project and external AI services or databases. Drivers are like adapters that let your project talk to specialized services.

    - **PromptDriver**: Communicates with AI text generators (like GPT models).
    - **WebSearchDriver**: Connects to search engines to find information.
    - **EmbeddingDriver**: Transforms words and concepts into numerical form that AI can understand.
    - **ImageGenerationDriver**: Connects to AI image generators (like DALL-E).
    - **AudioTranscriptionDriver**: Converts spoken audio to written text.

- **Engine**: The powerhouse that processes your creative requests. Engines are specialized components that transform inputs into creative outputs.

    - **RagEngine**: Processes questions and generates answers based on provided information.
    - **PromptSummaryEngine**: Creates concise summaries of longer text.
    - **CsvExtractionEngine**: Pulls information from spreadsheet-like files.
    - **JsonExtractionEngine**: Pulls information from structured data files.

- **Artifact**: A piece of content created during your project, like a generated image or text. Artifacts are the outputs of your creative process.

    - **ErrorArtifact**: A notification when something goes wrong.

- **Agent**: A helper that can perform tasks on your behalf, often using a combination of tools. Agents are like assistants that can navigate a sequence of operations for you.

- **Ruleset**: A set of guidelines that control how your project behaves. Rulesets are like recipes that tell your project how to respond in different situations.

    - **Rule**: A single instruction within a ruleset, like "if the user asks for an image, generate one."

### Node Contents and Activities

- **Parameter**: A setting or value you can adjust on a node. Parameters are like the knobs and sliders in a music synthesizer — they let you fine-tune how things work.

    - **ParameterMode**: Describes if a parameter is for input, output, internal, or any combination.

    - **ParameterValue**: The "internal" value for a parameter (this is the data the node works with internally)

    - **ParameterOutputValues**: The results or "output" value for your parameters (this is the data that results from what the node did)

    - **ParameterUIOptions**: Settings for how parameters appear in the user interface.

    - **Port**: The circular indicators displayed on the left and/or right sides of a parameter. Ports on the left side indicate the parameter can accept incoming connections, while ports on the right side indicate the parameter supports outgoing connections.

    - **Pin**: Interchangeable with "Port"

- **Default Value**: The pre-set value a parameter has before you change it. This is like the factory settings on a device.

- **Parameter Validation**: A check that ensures the values you enter make sense. This prevents errors like trying to use text where a number is needed.

- **Off-prompt**: A Griptape secret-sauce way to keep some information private, even when working with LLMs.

- **Stream Mode**: A continuous processing mode, like a live video feed rather than taking separate photos.

- **Type Hints**: Labels that suggest what kind of information a parameter expects. These are like labels on art supply containers telling you what's inside.

    - **Any**: A type hint meaning a parameter can accept any kind of information.
    - **List**: A type hint for a collection of items (like an array of colors or shapes).
    - **Literal**: A type hint indicating a parameter only accepts specific preset values.
    - **Union type**: A type hint showing a parameter can accept multiple specific types of information.

- **Connection**: The link that allows nodes to communicate with each other. Connections are like the cables connecting different pieces of equipment.

## Technical Terms Made Simple

- **Data Types**:

    - **Dictionary (dict)**: A way to store information as pairs of labels and values. Dictionaries are like organized containers where each item has a unique label.
    - **Key-Value Pair**: A label (key) paired with its corresponding information (value). Dictionaries are just a collection of Key-Value Pairs.
    - **Integer (int)**: Whole numbers without decimal points, like 5, -10, or 1000.
    - **Float (float)**: Numbers with decimal points, like 3.14, -0.5, or 2.0.
    - **String (str)**: Text enclosed in quotes, like "hello", 'Python', or "123".
    - **Boolean (bool)**: Represents either True or False.
    - **List (list)**: An ordered collection of items that can be modified, like [1, 2, 3] or ["apple", "banana", "cherry"].
    - **Tuple (tuple)**: An ordered collection of items that cannot be modified after creation, like (1, 2, 3) or ("red", "green", "blue").
    - **Set (set)**: An unordered collection of unique items, like {1, 2, 3} or {"apple", "banana", "cherry"}.
    - **None (NoneType)**: Represents the absence of a value or a null value.

- **Environment Variable**: Like secret notes that your computer keeps to help programs know important information.

- **API Key**: A special password that grants your project access to external services like AI image generators. Think of it like a membership card that lets you use specific online services.

<a id="secret-keys"></a>

- **Secret Keys**: Private credentials such as API tokens, passwords, and access keys that need to be kept secure but are required for certain operations.
- **Secrets Manager**: This is the actual mechanism by which your secret keys are handled so that they *stay* secret.

<a id="configuration-settings"></a>

- **Configuration Settings**: Parameters and options that control how the Griptape Nodes engine and its components operate.

<a id="project-files"></a>

- **Project Files**: Documents, scripts, and other resources that make up a Griptape project.

<a id="generated-assets"></a>

- **Generated Assets**: Files and data produced by the Griptape Nodes engine during execution, such as outputs, reports, or visualizations.

<a id=".env"></a>

- **.env**: A special file used to store environment variables, particularly sensitive information like API keys.

## AI Terms for Artists

- **Temperature Control**: A setting that controls how creative or predictable an AI's responses will be. Lower temperature means more predictable, higher temperature means more creative and varied.

- **Embedding Model**: An AI tool that converts words, images, or other content into numbers that capture their meaning. This helps AI understand relationships between different concepts.

- **LLM (Large Language Model)**: A type of AI system trained on vast amounts of text data to understand and generate human-like language. LLMs can write text, answer questions, summarize information, and even generate creative content.

- **NLP (Natural Language Processing)**: The field of AI focused on helping computers understand and generate human language.

- **Prompt**: The input text you provide to guide an AI model. For artists, this is similar to a creative brief or instructions to a collaborator.

- **Vector Store**: A specialized database that stores information in a way that captures relationships and meaning, not just the information itself.

- **Diffusion Models**: AI systems that create images by gradually transforming random noise into detailed visuals based on text descriptions. Popular examples include Stable Diffusion and Midjourney.

- **Text-to-Image Generation**: Technology that creates images based on written descriptions, allowing artists to visualize concepts through text prompts.

- **Style Transfer**: AI technique that applies the visual style of one image (like a famous painting) to the content of another image.

- **Inpainting/Outpainting**: Tools that can fill in missing parts of an image or extend it beyond its original borders based on surrounding context.

- **Fine-tuning**: The process of adapting a pre-trained AI model to recognize and generate specific styles, subjects, or artistic elements.

- **Latent Space**: A mathematical representation where AI models organize concepts; artists can explore this space to find creative variations and transitions between ideas.

- **Tokens**: The basic units that AI language models process text in, similar to words or parts of words. Understanding token limits helps artists craft effective prompts.

- **Multimodal AI**: Systems that can work with multiple types of content (text, images, audio) simultaneously, enabling more integrated creative workflows.

- **LoRA (Low-Rank Adaptation)**: A technique that allows artists to train AI models on their specific style without extensive computing resources.

- **Sampling Methods**: Different algorithms (like DDIM, Euler, etc.) that control how AI generates images, affecting detail levels, creativity, and coherence.

## AI Companies and Models (Updated Apr 9,2025)

### Anthropic

- **Claude 3 Opus**: Anthropic's most capable model for complex creative tasks and detailed content generation
- **Claude 3.5 Sonnet**: Balanced model offering strong creative capabilities with faster performance
- **Claude 3.7 Sonnet**: Advanced reasoning-focused model for complex creative problem-solving
- **Claude 3.5 Haiku**: Fastest model for rapid ideation and real-time creative collaboration

### OpenAI

- **GPT-4o**: OpenAI's most advanced multimodal model for text, image, and audio processing
- **GPT-4 Turbo**: Powerful large language model for sophisticated text generation and creative writing
- **GPT-3.5 Turbo**: More economical model for standard creative text tasks
- **DALL-E 3**: Text-to-image generation model for creating visual art from descriptions
- **Whisper**: Speech recognition model that can transcribe audio for creative projects

### Stability AI

- **Stable Diffusion XL**: Text-to-image model with high-quality image generation capabilities
- **Stable Diffusion 3**: Latest generation text-to-image model with improved coherence
- **Stable Video**: Text-to-video generation model for creating short animated sequences

### Google

- **Gemini Pro**: Google's advanced model for text generation and reasoning
- **Gemini Ultra**: Google's most capable model for complex creative tasks
- **Imagen**: Text-to-image generation model with photorealistic capabilities

### Cohere

- **Command**: Specialized for instruction following and precise creative tasks
- **Embed**: Creates semantic representations of text for organizing creative content

### Midjourney

- **Midjourney V6**: Image generation model popular among artists for its distinctive aesthetic

### Meta

- **Llama 3**: Open-weight foundation model adaptable for various creative applications
- **Llama 3.1**: More advanced version with improved capabilities
- **Segment Anything**: AI model for precise image segmentation in visual arts

### Ollama

- **Local model hosting service**: Allows running various open-source AI models locally for creative projects

- -

## Helpful Programming Concepts

- **Callback**: A function that automatically runs when something specific happens. This is like setting up a camera to take a photo when motion is detected.

- **Custom Parameter Types**: Specialized settings you can define for your specific project needs.

- **Exception Handling**: The way your project deals with errors. This is like having backup plans for when things go wrong.

    - **Authentication Error**: An error that occurs when your project can't prove it has permission to use a service.
    - **KeyError**: An error when your project tries to access information using an incorrect label.
    - **SyntaxError**: An error caused by incorrect formatting in code.
    - **ValueError**: An error when your project tries to use an inappropriate value.

- **Metadata**: Extra information about your content or components. This is like the information stored in digital photo files (camera type, date taken, etc.).

## Additional Terms

- **Python**: A popular programming language that Griptape is built with. Python is known for being relatively easy to read and understand compared to other programming languages.

- **API (Application Programming Interface)**: A set of rules that allows different software applications to communicate with each other. For artists, this is like the standardized connections that allow different audio equipment to work together.

- **JSON (JavaScript Object Notation)**: A common format for storing and transmitting data. This is like a standardized template for organizing information.

- **CSV (Comma-Separated Values)**: A simple file format used to store tabular data, such as spreadsheet data. Each line represents a row, and commas separate the values in each column.

- **UI (User Interface)**: The visual elements and controls that allow users to interact with software. This includes buttons, sliders, text fields, and other interactive elements. In Griptape Nodes, we call our UI the "Editor"

- **Framework**: A pre-built set of code that provides structure and functionality to build applications more easily. Frameworks are like art supply kits that come with the basic materials and tools you need.
