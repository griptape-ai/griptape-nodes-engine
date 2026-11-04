# JSON Extract Value

Extract values from JSON using [JMESPath](https://jmespath.org) expressions.

## Description

The JSON Extract Value node allows you to extract specific values from JSON data using JMESPath expressions. It supports nested object access, array indexing, and powerful wildcard operations, making it easy to navigate and extract data from complex JSON structures.

## Parameters

### Input Parameters

| Parameter | Type            | Description                                                                                | Default |
| --------- | --------------- | ------------------------------------------------------------------------------------------ | ------- |
| `json`    | json, str, dict | The JSON data to extract from                                                              | `{}`    |
| `path`    | str             | JMESPath expression to extract data (e.g., 'user.name', 'items[0].title', '[\*].assignee') | `""`    |

### Output Parameters

| Parameter | Type | Description         |
| --------- | ---- | ------------------- |
| `output`  | json | The extracted value |

## Features

- **JMESPath Expressions**: Use powerful JMESPath syntax for flexible data extraction
- **Dot Notation Paths**: Use simple dot notation to navigate JSON structure
- **Array Indexing**: Access array elements using `[index]` syntax
- **Wildcard Operations**: Extract all values from arrays using `[*]` syntax
- **Native Python Types**: Returns raw Python values (strings, dicts, lists, etc.) - no JSON serialization needed
- **Real-time Updates**: Automatically updates when input changes
- **Safe Extraction**: Returns empty dict `{}` if path doesn't exist

## JMESPath Syntax

> **📚 Learn More**: For complete JMESPath syntax and advanced features, see the [JMESPath Documentation](https://jmespath.org/tutorial.html).

### Basic Object Access

```python
# Access nested object properties
path = "user.name"  # Gets user's name
path = "user.profile.email"  # Gets nested email
```

### Array Indexing

```python
# Access array elements
path = "items[0]"  # Gets first item
path = "items[0].title"  # Gets title of first item
path = "users[2].name"  # Gets name of third user
```

### Wildcard Operations

```python
# Extract all values from arrays
path = "items[*].title"  # Gets all titles from items array
path = "users[*].name"  # Gets all names from users array
path = "[*].assignee"  # Gets all assignees from root array
```

### Complex Paths

```python
# Combine object and array access
path = "orders[0].items[1].price"  # Gets price of second item in first order
path = "projects[*].tasks[*].assignee"  # Gets all assignees from all project tasks
```

## Examples

### Basic Object Extraction

```python
# Input JSON: {"user": {"name": "John", "age": 30}}
# Path: "user.name"
# Output: "John"  # Returns raw Python string value
```

### Array Element Extraction

```python
# Input JSON: {"items": [{"title": "Book", "price": 25}, {"title": "Magazine", "price": 10}]}
# Path: "items[0].title"
# Output: "Book"  # Returns raw Python string value
```

### Nested Array Access

```python
# Input JSON: {"orders": [{"items": [{"name": "Product A"}, {"name": "Product B"}]}]}
# Path: "orders[0].items[1].name"
# Output: "Product B"  # Returns raw Python string value
```

### Non-existent Path

```python
# Input JSON: {"user": {"name": "John"}}
# Path: "user.email"
# Output: {}  # Empty dict (Python dict object) for non-existent paths
```

### Different Return Types

The node returns raw Python values directly from JMESPath, matching the extracted data type:

```python
# String extraction
# Input JSON: {"product": {"name": "Widget", "category": "Electronics"}}
# Path: "product.name"
# Output: "Widget"  # Python string

# Object extraction
# Input JSON: {"user": {"name": "John", "age": 30}}
# Path: "user"
# Output: {"name": "John", "age": 30}  # Python dict

# Array extraction
# Input JSON: {"items": [{"title": "Book"}, {"title": "Magazine"}]}
# Path: "items"
# Output: [{"title": "Book"}, {"title": "Magazine"}]  # Python list
```

**Important Notes:**

- The node returns native Python types (str, dict, list, int, bool, etc.) - no JSON serialization
- This makes the output directly usable by other nodes without parsing
- Consistent with other JSON nodes in the library (JsonInput, JsonReplace, etc.)
- JMESPath handles all type conversions automatically

## Use Cases

- **API Response Processing**: Extract specific fields from API responses
- **Data Filtering**: Get only the data you need from large JSON objects
- **Configuration Access**: Extract specific settings from configuration files
- **Data Transformation**: Prepare data for other nodes in your workflow

## Related Nodes

- [JSON Input](json_input.md) - Create JSON data from inputs
- [JSON Replace](json_replace.md) - Replace values in JSON
- [Display JSON](display_json.md) - Display and format JSON data
- [To JSON](../convert/to_json.md) - Convert other data types to JSON
