package com.controlvar.sdk.model;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.HashMap;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for {@link User} and its {@link User.Builder}.
 */
@DisplayName("User")
class UserTest {

    @Test
    @DisplayName("builder creates User with correct userId")
    void testUserBuilderCreatesCorrectUserId() {
        User user = User.builder("user-123").build();
        assertEquals("user-123", user.getUserId());
    }

    @Test
    @DisplayName("builder creates User with correct attributes")
    void testUserBuilderCreatesCorrectUser() {
        User user = User.builder("user-456")
                .attribute("country", "US")
                .attribute("plan", "premium")
                .attribute("age", 30)
                .build();

        assertEquals("user-456", user.getUserId());
        assertEquals("US",      user.getAttribute("country"));
        assertEquals("premium", user.getAttribute("plan"));
        assertEquals(30,        user.getAttribute("age"));
        assertEquals(3,         user.getAttributes().size());
    }

    @Test
    @DisplayName("builder creates User with empty attributes when none added")
    void testUserBuilderCreatesEmptyAttributesByDefault() {
        User user = User.builder("user-789").build();
        assertNotNull(user.getAttributes());
        assertTrue(user.getAttributes().isEmpty(),
                "Attributes map should be empty when no attributes were added");
    }

    @Test
    @DisplayName("constructor throws IllegalArgumentException when userId is null")
    void testUserIdRequired() {
        assertThrows(IllegalArgumentException.class,
                () -> User.builder(null).build(),
                "Null userId should throw IllegalArgumentException");
    }

    @Test
    @DisplayName("constructor throws IllegalArgumentException when userId is empty string")
    void testUserIdCannotBeEmpty() {
        assertThrows(IllegalArgumentException.class,
                () -> User.builder("").build(),
                "Empty userId should throw IllegalArgumentException");
    }

    @Test
    @DisplayName("getAttributes returns an immutable view")
    void testUserAttributesAreImmutable() {
        User user = User.builder("user-123")
                .attribute("country", "US")
                .build();

        Map<String, Object> attrs = user.getAttributes();
        assertThrows(UnsupportedOperationException.class,
                () -> attrs.put("newKey", "newValue"),
                "getAttributes() should return an unmodifiable map");
    }

    @Test
    @DisplayName("modifying the builder map after build does not affect User")
    void testBuilderMapIsolatedFromUser() {
        User.Builder builder = User.builder("user-123");
        builder.attribute("country", "US");
        User user = builder.build();

        // Build a second user from the same builder after modification
        builder.attribute("country", "DE"); // mutate builder
        User user2 = builder.build();

        // user should be unchanged
        assertEquals("US", user.getAttribute("country"),
                "Original user's country should not be affected by later builder modifications");
        assertEquals("DE", user2.getAttribute("country"),
                "New user should reflect the modified builder state");
    }

    @Test
    @DisplayName("getAttribute returns null for an attribute that does not exist")
    void testGetAttributeReturnsNullForMissingKey() {
        User user = User.builder("user-123").attribute("country", "US").build();
        assertNull(user.getAttribute("nonexistent"),
                "getAttribute should return null for missing key");
    }

    @Test
    @DisplayName("attributes(Map) bulk-loads all entries from the provided map")
    void testAttributesBulkLoad() {
        Map<String, Object> attrs = new HashMap<>();
        attrs.put("country", "CA");
        attrs.put("tier", "gold");

        User user = User.builder("user-123").attributes(attrs).build();

        assertEquals("CA",   user.getAttribute("country"));
        assertEquals("gold", user.getAttribute("tier"));
        assertEquals(2,      user.getAttributes().size());
    }

    @Test
    @DisplayName("attributes(null) is silently ignored (no NPE)")
    void testAttributesNullMapIsIgnored() {
        assertDoesNotThrow(() -> {
            User user = User.builder("user-123").attributes(null).build();
            assertTrue(user.getAttributes().isEmpty());
        });
    }

    @Test
    @DisplayName("attribute key validation: null key throws IllegalArgumentException")
    void testAttributeNullKeyThrows() {
        assertThrows(IllegalArgumentException.class,
                () -> User.builder("user-123").attribute(null, "value").build());
    }

    @Test
    @DisplayName("attribute key validation: empty key throws IllegalArgumentException")
    void testAttributeEmptyKeyThrows() {
        assertThrows(IllegalArgumentException.class,
                () -> User.builder("user-123").attribute("", "value").build());
    }

    @Test
    @DisplayName("equals returns true for Users with same userId and attributes")
    void testEquality() {
        User u1 = User.builder("user-123").attribute("country", "US").build();
        User u2 = User.builder("user-123").attribute("country", "US").build();
        assertEquals(u1, u2, "Users with same userId and attributes should be equal");
    }

    @Test
    @DisplayName("equals returns false for Users with different userIds")
    void testInequalityDifferentUserId() {
        User u1 = User.builder("user-123").build();
        User u2 = User.builder("user-456").build();
        assertNotEquals(u1, u2);
    }

    @Test
    @DisplayName("hashCode is consistent with equals")
    void testHashCodeConsistency() {
        User u1 = User.builder("user-123").attribute("country", "US").build();
        User u2 = User.builder("user-123").attribute("country", "US").build();
        assertEquals(u1.hashCode(), u2.hashCode(),
                "Equal users should have equal hash codes");
    }

    @Test
    @DisplayName("toString contains userId")
    void testToStringContainsUserId() {
        User user = User.builder("user-123").build();
        assertTrue(user.toString().contains("user-123"),
                "toString should contain the userId");
    }
}
