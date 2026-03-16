"""Data structures for contextual help system."""

from dataclasses import dataclass, field


@dataclass
class HelpTip:
    """A single help tip with 2-4 sentences of guidance."""

    text: str


@dataclass
class HelpEntry:
    """Help entry with one or more tips (multiple tips enable pagination)."""

    tips: list[HelpTip] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.tips:
            raise ValueError("HelpEntry requires at least one HelpTip")

    @property
    def page_count(self) -> int:
        """Number of pages (tips) available."""
        return len(self.tips)

    def get_tip(self, page: int) -> HelpTip:
        """Get tip for given page (wraps around)."""
        return self.tips[page % len(self.tips)]
