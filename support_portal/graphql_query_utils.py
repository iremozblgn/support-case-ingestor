from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Token:
    value: str
    kind: str


@dataclass(slots=True)
class FieldNode:
    alias: str | None
    name: str
    arguments: list[str] = field(default_factory=list)
    directives: list[str] = field(default_factory=list)
    selection_set: list["SelectionNode"] | None = None


@dataclass(slots=True)
class InlineFragmentNode:
    type_condition: str | None
    directives: list[str] = field(default_factory=list)
    selection_set: list["SelectionNode"] = field(default_factory=list)


@dataclass(slots=True)
class FragmentSpreadNode:
    name: str
    directives: list[str] = field(default_factory=list)


SelectionNode = FieldNode | InlineFragmentNode | FragmentSpreadNode


@dataclass(slots=True)
class DefinitionNode:
    header_tokens: list[str]
    selection_set: list[SelectionNode]


@dataclass(slots=True)
class DocumentNode:
    definitions: list[DefinitionNode]


class GraphQLQueryParseError(ValueError):
    """Raised when the query mutation utility cannot parse the GraphQL query."""


PUNCTUATORS = set("!$():=@[]{|},")


def tokenize_graphql(query: str) -> list[Token]:
    tokens: list[Token] = []
    index = 0
    length = len(query)

    while index < length:
        char = query[index]
        if char.isspace() or char == ",":
            index += 1
            continue
        if char == "#":
            while index < length and query[index] != "\n":
                index += 1
            continue
        if query.startswith("...", index):
            tokens.append(Token("...", "ELLIPSIS"))
            index += 3
            continue
        if char == '"':
            start = index
            index += 1
            while index < length:
                if query[index] == "\\":
                    index += 2
                    continue
                if query[index] == '"':
                    index += 1
                    break
                index += 1
            tokens.append(Token(query[start:index], "STRING"))
            continue
        if char in PUNCTUATORS or char in "{}":
            tokens.append(Token(char, "PUNCT"))
            index += 1
            continue
        if char.isalpha() or char == "_":
            start = index
            index += 1
            while index < length and (query[index].isalnum() or query[index] == "_"):
                index += 1
            tokens.append(Token(query[start:index], "NAME"))
            continue
        if char.isdigit() or char == "-":
            start = index
            index += 1
            while index < length and (query[index].isdigit() or query[index] == "."):
                index += 1
            tokens.append(Token(query[start:index], "NUMBER"))
            continue
        raise GraphQLQueryParseError(f"Unexpected character in GraphQL query: {char!r}")

    return tokens


class GraphQLParser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.index = 0

    def parse_document(self) -> DocumentNode:
        definitions: list[DefinitionNode] = []
        while not self._at_end():
            definitions.append(self._parse_definition())
        return DocumentNode(definitions=definitions)

    def _parse_definition(self) -> DefinitionNode:
        header_tokens: list[str] = []
        paren_depth = 0
        bracket_depth = 0
        while not self._at_end():
            token = self._peek()
            if token.value == "(":
                paren_depth += 1
            elif token.value == ")":
                paren_depth -= 1
            elif token.value == "[":
                bracket_depth += 1
            elif token.value == "]":
                bracket_depth -= 1
            elif token.value == "{" and paren_depth == 0 and bracket_depth == 0:
                break
            header_tokens.append(token.value)
            self.index += 1

        selection_set = self._parse_selection_set()
        return DefinitionNode(header_tokens=header_tokens, selection_set=selection_set)

    def _parse_selection_set(self) -> list[SelectionNode]:
        self._expect("{")
        selections: list[SelectionNode] = []
        while not self._at_end() and self._peek().value != "}":
            selections.append(self._parse_selection())
        self._expect("}")
        return selections

    def _parse_selection(self) -> SelectionNode:
        token = self._peek()
        if token.value == "...":
            self.index += 1
            if not self._at_end() and self._peek().value == "on":
                self.index += 1
                type_condition = self._consume_name()
                directives = self._consume_directives()
                selection_set = self._parse_selection_set()
                return InlineFragmentNode(
                    type_condition=type_condition,
                    directives=directives,
                    selection_set=selection_set,
                )
            name = self._consume_name()
            directives = self._consume_directives()
            return FragmentSpreadNode(name=name, directives=directives)
        return self._parse_field()

    def _parse_field(self) -> FieldNode:
        first_name = self._consume_name()
        alias: str | None = None
        name = first_name
        if not self._at_end() and self._peek().value == ":":
            self.index += 1
            alias = first_name
            name = self._consume_name()

        arguments = self._consume_group("(") if self._peek_value("(") else []
        directives = self._consume_directives()
        selection_set = self._parse_selection_set() if self._peek_value("{") else None
        return FieldNode(
            alias=alias,
            name=name,
            arguments=arguments,
            directives=directives,
            selection_set=selection_set,
        )

    def _consume_directives(self) -> list[str]:
        directives: list[str] = []
        while self._peek_value("@"):
            directives.append(self._expect("@").value)
            directives.append(self._consume_name())
            if self._peek_value("("):
                directives.extend(self._consume_group("("))
        return directives

    def _consume_group(self, opening: str) -> list[str]:
        closing = { "(": ")", "[": "]" }[opening]
        tokens: list[str] = []
        depth = 0
        while not self._at_end():
            token = self._peek()
            tokens.append(token.value)
            self.index += 1
            if token.value == opening:
                depth += 1
            elif token.value == closing:
                depth -= 1
                if depth == 0:
                    break
        if depth != 0:
            raise GraphQLQueryParseError(f"Unbalanced group starting with {opening!r}")
        return tokens

    def _consume_name(self) -> str:
        token = self._peek()
        if token.kind != "NAME":
            raise GraphQLQueryParseError(f"Expected NAME token, got {token.value!r}")
        self.index += 1
        return token.value

    def _expect(self, value: str) -> Token:
        token = self._peek()
        if token.value != value:
            raise GraphQLQueryParseError(f"Expected {value!r}, got {token.value!r}")
        self.index += 1
        return token

    def _peek(self) -> Token:
        if self._at_end():
            raise GraphQLQueryParseError("Unexpected end of GraphQL query")
        return self.tokens[self.index]

    def _peek_value(self, value: str) -> bool:
        return not self._at_end() and self.tokens[self.index].value == value

    def _at_end(self) -> bool:
        return self.index >= len(self.tokens)


def parse_graphql_document(query: str) -> DocumentNode:
    parser = GraphQLParser(tokenize_graphql(query))
    return parser.parse_document()


def remove_graphql_field(query: str, field_name: str) -> str:
    document = parse_graphql_document(query)
    changed = False
    for definition in document.definitions:
        definition.selection_set, removed = _remove_from_selection_set(
            definition.selection_set,
            field_name=field_name,
        )
        changed = changed or removed
    if not changed:
        return query
    return render_graphql_document(document)


def list_graphql_field_names(query: str) -> list[str]:
    document = parse_graphql_document(query)
    field_names: set[str] = set()
    for definition in document.definitions:
        _collect_field_names(definition.selection_set, field_names)
    return sorted(field_names)


def render_graphql_document(document: DocumentNode) -> str:
    return "\n".join(
        _render_definition(definition)
        for definition in document.definitions
    )


def _remove_from_selection_set(
    selection_set: list[SelectionNode],
    *,
    field_name: str,
) -> tuple[list[SelectionNode], bool]:
    changed = False
    updated: list[SelectionNode] = []
    for selection in selection_set:
        if isinstance(selection, FieldNode):
            if selection.name == field_name:
                changed = True
                continue
            if selection.selection_set is not None:
                selection.selection_set, nested_changed = _remove_from_selection_set(
                    selection.selection_set,
                    field_name=field_name,
                )
                changed = changed or nested_changed
        elif isinstance(selection, InlineFragmentNode):
            selection.selection_set, nested_changed = _remove_from_selection_set(
                selection.selection_set,
                field_name=field_name,
            )
            changed = changed or nested_changed
        updated.append(selection)
    return updated, changed


def _collect_field_names(selection_set: list[SelectionNode], field_names: set[str]) -> None:
    for selection in selection_set:
        if isinstance(selection, FieldNode):
            field_names.add(selection.name)
            if selection.selection_set is not None:
                _collect_field_names(selection.selection_set, field_names)
        elif isinstance(selection, InlineFragmentNode):
            _collect_field_names(selection.selection_set, field_names)


def _render_definition(definition: DefinitionNode) -> str:
    header = _render_token_sequence(definition.header_tokens)
    selection = _render_selection_set(definition.selection_set)
    if header:
        return f"{header} {selection}"
    return selection


def _render_selection_set(selection_set: list[SelectionNode]) -> str:
    inner = " ".join(_render_selection(selection) for selection in selection_set)
    return "{ " + inner + " }"


def _render_selection(selection: SelectionNode) -> str:
    if isinstance(selection, FieldNode):
        alias = f"{selection.alias}: " if selection.alias else ""
        arguments = _render_token_sequence(selection.arguments)
        directives = _render_token_sequence(selection.directives)
        rendered = f"{alias}{selection.name}"
        if arguments:
            rendered += arguments
        if directives:
            rendered += f" {directives}"
        if selection.selection_set is not None:
            rendered += f" {_render_selection_set(selection.selection_set)}"
        return rendered
    if isinstance(selection, InlineFragmentNode):
        type_condition = f" on {selection.type_condition}" if selection.type_condition else ""
        directives = _render_token_sequence(selection.directives)
        rendered = f"...{type_condition}"
        if directives:
            rendered += f" {directives}"
        rendered += f" {_render_selection_set(selection.selection_set)}"
        return rendered
    directives = _render_token_sequence(selection.directives)
    rendered = f"... {selection.name}"
    if directives:
        rendered += f" {directives}"
    return rendered


def _render_token_sequence(tokens: list[str]) -> str:
    if not tokens:
        return ""
    output = tokens[0]
    for token in tokens[1:]:
        if _needs_space(output, token):
            output += " "
        output += token
    return output


def _needs_space(current: str, next_token: str) -> bool:
    if not current:
        return False
    if current.endswith(("(", "[", "{", ":", "@", "$")):
        return False
    if next_token in {")", "]", "}", ":", "@", ","}:
        return False
    if next_token == "...":
        return True
    if current.endswith("..."):
        return True
    return True
