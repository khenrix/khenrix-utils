def canonical_id(value):
    if not isinstance(value, str):
        raise TypeError("ID must be a string")
    result = value.strip().lower()
    if not result:
        raise ValueError("ID must not be blank")
    return result
