# JSON Find

Find items in JSON arrays based on search criteria.

## Description

The JSON Find node allows you to search through JSON arrays and find items that match specific criteria. It supports multiple search modes, case sensitivity options, and can return either the first match or all matches. Perfect for filtering and searching through complex data structures.

## Parameters

### Input Parameters

| Parameter        | Type            | Description                                                            | Default   |
| ---------------- | --------------- | ---------------------------------------------------------------------- | --------- |
| `json`           | json, str, dict | The JSON data to search through (should be an array or contain arrays) | `[]`      |
| `search_field`   | str             | Dot notation path to the field to search (e.g., 'attributes.content')  | `""`      |
| `search_value`   | str             | Value to search for                                                    | `""`      |
| `search_mode`    | str             | Search mode: 'exact', 'contains', or 'starts_with'                     | `"exact"` |
| `return_mode`    | str             | Return mode: 'first' for first match, 'all' for all matches            | `"first"` |
| `case_sensitive` | bool            | Whether the search should be case sensitive                            | `true`    |

### Output Parameters

| Parameter     | Type | Description                                                               |
| ------------- | ---- | ------------------------------------------------------------------------- |
| `found_item`  | json | The found item(s) - single item if return_mode is 'first', array if 'all' |
| `found_count` | int  | Number of items found                                                     |
| `found_index` | int  | Index of the first found item (or -1 if not found)                        |

## Features

- **Flexible Search Modes**: Exact match, contains, or starts with
- **Case Sensitivity Control**: Choose case-sensitive or case-insensitive search
- **Multiple Return Options**: Get first match or all matches
- **Nested Field Access**: Use dot notation to search deep into JSON structures
- **Array Auto-Detection**: Automatically finds arrays in common field names
- **Real-time Updates**: Automatically updates when input changes

## Search Modes

### Exact Match (`exact`)

Finds items where the field value exactly matches the search value.

```python
# Search for: "Design"
# Matches: "Design"
# Does not match: "Design Task", "design", "My Design"
```

### Contains (`contains`)

Finds items where the field value contains the search value as a substring.

```python
# Search for: "Design"
# Matches: "Design", "Design Task", "My Design Work"
# Does not match: "design" (if case_sensitive=true)
```

### Starts With (`starts_with`)

Finds items where the field value starts with the search value.

```python
# Search for: "Design"
# Matches: "Design", "Design Task", "Designer"
# Does not match: "My Design", "design"
```

## Field Path Syntax

The `search_field` parameter uses the same dot notation as JSON Extract Value:

### Basic Object Access

```python
search_field = "name"  # Search in root name field
search_field = "attributes.content"  # Search in nested content field
search_field = "user.profile.email"  # Search in deeply nested email field
```

### Array Indexing

```python
search_field = "items[0].title"  # Search in title of first item
search_field = "users[2].name"  # Search in name of third user
```

## Examples

### Find Task by Content

```python
# Input JSON: [
#   {"attributes": {"content": "Design", "status": "wtg"}},
#   {"attributes": {"content": "Model", "status": "wtg"}},
#   {"attributes": {"content": "Design Review", "status": "ip"}}
# ]
# search_field: "attributes.content"
# search_value: "Design"
# search_mode: "exact"
# return_mode: "first"
# Result: {"attributes": {"content": "Design", "status": "wtg"}}
```

### Find All Tasks with Status

```python
# Input JSON: [
#   {"attributes": {"content": "Design", "status": "wtg"}},
#   {"attributes": {"content": "Model", "status": "wtg"}},
#   {"attributes": {"content": "Review", "status": "ip"}}
# ]
# search_field: "attributes.status"
# search_value: "wtg"
# search_mode: "exact"
# return_mode: "all"
# Result: [{"attributes": {"content": "Design", "status": "wtg"}},
#          {"attributes": {"content": "Model", "status": "wtg"}}]
```

### Case-Insensitive Search

```python
# Input JSON: [
#   {"name": "Design Task"},
#   {"name": "design review"},
#   {"name": "DESIGN WORK"}
# ]
# search_field: "name"
# search_value: "design"
# search_mode: "contains"
# case_sensitive: false
# return_mode: "all"
# Result: All three items (matches "Design", "design", "DESIGN")
```

### Find Items Starting With

```python
# Input JSON: [
#   {"title": "Design Task"},
#   {"title": "Design Review"},
#   {"title": "My Design Work"}
# ]
# search_field: "title"
# search_value: "Design"
# search_mode: "starts_with"
# return_mode: "all"
# Result: [{"title": "Design Task"}, {"title": "Design Review"}]
```

### Complex Nested Search

```python
# Input JSON: [
#   {
#     "relationships": {
#       "entity": {"data": {"name": "convertible", "type": "Asset"}}
#     }
#   }
# ]
# search_field: "relationships.entity.data.name"
# search_value: "convertible"
# search_mode: "exact"
# return_mode: "first"
# Result: The matching item
```

## Array Auto-Detection

If your input JSON is an object containing arrays, the node will automatically look for common array field names:

- `data`
- `items`
- `results`
- `list`

```python
# Input JSON: {"data": [{"name": "Item 1"}, {"name": "Item 2"}]}
# The node will automatically search in the "data" array
```

## Use Cases

- **Task Management**: Find specific tasks by content, status, or assignee
- **Data Filtering**: Filter large datasets based on specific criteria
- **API Response Processing**: Search through API response arrays
- **Configuration Lookup**: Find specific configuration items
- **Content Discovery**: Search through content collections
- **User Management**: Find users by name, email, or role

## Related Nodes

- [JSON Extract Value](json_extract_value.md) - Extract single values by path
- [JSON Input](json_input.md) - Create JSON data from inputs
- [JSON Replace](json_replace.md) - Replace values in JSON
- [Display JSON](display_json.md) - Display and format JSON data
- [To JSON](../convert/to_json.md) - Convert other data types to JSON
