"""Public synthetic quoted-field parser, deliberately flawed."""
def parse_record(text):
    return text.rstrip('\n').split(',')
