package com.controlvar.sdk.model;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for the {@code event_id} of a {@link TrackEvent} (#1543): the id set with
 * {@link TrackEvent.Builder#eventId(String)}, its wire field, and the ids the server would
 * refuse, which the builder refuses first.
 */
@DisplayName("TrackEvent event id")
class TrackEventTest {

    /** A character outside the Basic Multilingual Plane: one code point, two Java chars. */
    private static final String FOUR_BYTE = "😀";

    @Test
    @DisplayName("toBody carries no event_id when none is set")
    void eventIdIsAbsentFromTheBodyWhenNoneIsSet() {
        TrackEvent event = TrackEvent.builder("u1", "purchase").experimentKey("e").build();

        assertNull(event.getEventId());
        assertFalse(event.toBody().containsKey("event_id"));
    }

    @Test
    @DisplayName("toBody carries the event_id that was set, unchanged")
    void eventIdIsSentAsSet() {
        TrackEvent event = TrackEvent.builder("u1", "purchase").eventId("Order-42 ").build();

        assertEquals("Order-42 ", event.getEventId());
        Map<String, Object> body = event.toBody();
        assertEquals("Order-42 ", body.get("event_id"), "sent exactly: not trimmed, case kept");
    }

    @Test
    @DisplayName("null and \"\" set no event_id")
    void nullAndEmptyEventIdSetNone() {
        assertNull(TrackEvent.builder("u1", "click").eventId(null).build().getEventId());
        assertNull(TrackEvent.builder("u1", "click").eventId("").build().getEventId());
        assertNull(TrackEvent.builder("u1", "click").eventId("x").eventId("").build().getEventId(),
                "a later \"\" clears an earlier id");
        assertFalse(TrackEvent.builder("u1", "click").eventId("").build().toBody().containsKey("event_id"));
    }

    @Test
    @DisplayName("255 characters are accepted, counted in code points as the server counts them")
    void eventIdOf255CharactersIsAccepted() {
        String ascii = repeat("a", 255);
        String wide = repeat(FOUR_BYTE, 255); // 510 Java chars, 255 characters

        assertEquals(ascii, TrackEvent.builder("u1", "click").eventId(ascii).build().getEventId());
        assertEquals(wide, TrackEvent.builder("u1", "click").eventId(wide).build().getEventId());
    }

    @Test
    @DisplayName("an event_id the server would refuse is refused by the builder: the event cannot be sent")
    void eventIdTheServerWouldRefuseIsRefused() {
        for (String refused : new String[] {
                repeat("a", 256),
                repeat(FOUR_BYTE, 256),
                "order" + '\0' + "42",
                "lone-high-\uD83D",
                "\uDE00-lone-low",
                "\uDE00\uD83D", // a low then a high surrogate: two lone halves, not a pair
        }) {
            IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                    () -> TrackEvent.builder("u1", "click").eventId(refused),
                    "refused: " + refused.length() + " chars");
            assertTrue(e.getMessage().startsWith("eventId must"), e.getMessage());
        }
    }

    @Test
    @DisplayName("equals and hashCode include the event id")
    void eventIdIsPartOfEquality() {
        TrackEvent a = TrackEvent.builder("u1", "click").eventId("id-1").build();
        TrackEvent b = TrackEvent.builder("u1", "click").eventId("id-1").build();
        TrackEvent c = TrackEvent.builder("u1", "click").eventId("id-2").build();

        assertEquals(a, b);
        assertEquals(a.hashCode(), b.hashCode());
        assertNotEquals(a, c);
        assertTrue(a.toString().contains("eventId='id-1'"), a.toString());
    }

    private static String repeat(String s, int times) {
        StringBuilder out = new StringBuilder();
        for (int i = 0; i < times; i++) out.append(s);
        return out.toString();
    }
}
