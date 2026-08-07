# JSON Replace

Replace values in JSON using dot notation paths.

## Description

The JSON Replace node allows you to modify JSON data by replacing values at specific paths using dot notation. It creates a deep copy of the original data to avoid modifying the source, and can automatically create missing paths if they don't exist.

## Parameters

### Input Parameters

| Parameter           | Type            | Description                                                        | Default |
| ------------------- | --------------- | ------------------------------------------------------------------ | ------- |
| `json`              | json, str, dict | The JSON data to modify                                            | `{}`    |
| `path`              | str             | Dot notation path to replace (e.g., 'user.name', 'items[0].title') | `""`    |
| `replacement_value` | json, str, dict | The new value to put at the specified path                         | `""`    |

### Output Parameters

| Parameter | Type | Description                                  |
| --------- | ---- | -------------------------------------------- |
| `output`  | json | The modified JSON with the replacement value |

## Features

- **Deep Copy Protection**: Creates a deep copy to avoid modifying the original data
- **Path Creation**: Automatically creates missing paths if they don't exist
- **Array Support**: Handles array indexing like `items[0].name`
- **Real-time Updates**: Updates automatically when any input parameter changes
- **Safe Operations**: Graceful handling of invalid paths or data types

## Path Syntax

### Basic Object Replacement

```python
# Replace nested object properties
path = "user.name"  # Replace user's name
path = "user.profile.email"  # Replace nested email
```

### Array Element Replacement

```python
# Replace array elements
path = "items[0]"  # Replace first item
path = "items[0].title"  # Replace title of first item
path = "users[2].name"  # Replace name of third user
```

### Complex Path Replacement

```python
# Replace in nested arrays
path = "orders[0].items[1].price"  # Replace price of second item in first order
```

## Examples

### Basic Value Replacement

```python
# Original JSON: {"user": {"name": "John", "age": 30}}
# Path: "user.name"
# Replacement Value: "Jane"
# Output: {"user": {"name": "Jane", "age": 30}}
```

### Array Element Replacement

```python
# Original JSON: {"items": [{"title": "Book", "price": 25}, {"title": "Magazine", "price": 10}]}
# Path: "items[0].title"
# Replacement Value: "Novel"
# Output: {"items": [{"title": "Novel", "price": 25}, {"title": "Magazine", "price": 10}]}
```

### Creating New Paths

```python
# Original JSON: {"user": {"name": "John"}}
# Path: "user.email"
# Replacement Value: "john@example.com"
# Output: {"user": {"name": "John", "email": "john@example.com"}}
```

### Nested Array Replacement

```python
# Original JSON: {"orders": [{"items": [{"name": "Product A"}, {"name": "Product B"}]}]}
# Path: "orders[0].items[1].name"
# Replacement Value: "Product C"
# Output: {"orders": [{"items": [{"name": "Product A"}, {"name": "Product C"}]}]}
```

### Extending Arrays

```python
# Original JSON: {"items": [{"title": "Book"}]}
# Path: "items[2].title"
# Replacement Value: "Magazine"
# Output: {"items": [{"title": "Book"}, null, {"title": "Magazine"}]}
```

## Use Cases

- **Data Updates**: Modify specific fields in configuration or user data
- **API Integration**: Update request payloads with new values
- **Data Transformation**: Modify JSON structure for different systems
- **Template Processing**: Fill in template JSON with dynamic values
- **Configuration Management**: Update settings in JSON configuration files

## Related Nodes

- [JSON Input](json_input.md) - Create JSON data from inputs
- [JSON Extract Value](json_extract_value.md) - Extract values from JSON
- [Display JSON](display_json.md) - Display and format JSON data
- [To JSON](../convert/to_json.md) - Convert other data types to JSON
