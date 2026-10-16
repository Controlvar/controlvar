package com.controlvar.android

import org.json.JSONObject

/**
 * Offline fallback storage for server results (flag evaluations and experiment
 * assignments), persisted per user + key.
 *
 * In production, backed by Android SharedPreferences (see [PrefsAdapter]).
 * In test environments (no Android context), falls back to an in-memory map.
 * Uses a SharedPreferences interface abstraction so both paths share the same logic.
 *
 * Only successful server results are ever written; [ExperimentationClient] reads
 * them back when the API cannot be reached.
 */
class OfflineStore(
    private val prefs: PrefsAdapter? = null  // null = use in-memory for tests
) {
    /**
     * Minimal SharedPreferences-like abstraction allowing test-time substitution.
     *
     * A production implementation wraps `context.getSharedPreferences(...)`:
     * `getString` -> `prefs.getString(key, null)`, `putString` -> `prefs.edit().putString(key, value).apply()`,
     * `remove` -> `prefs.edit().remove(key).apply()`, `allKeys` -> `prefs.all.keys`,
     * `clear` -> `prefs.edit().clear().apply()`.
     */
    interface PrefsAdapter {
        fun getString(key: String): String?
        fun putString(key: String, value: String)
        fun remove(key: String)
        fun allKeys(): Set<String>
        fun clear()
    }

    private val inMemory = mutableMapOf<String, String>()

    private fun rawGet(key: String): String? =
        prefs?.getString(key) ?: inMemory[key]

    private fun rawPut(key: String, value: String) {
        prefs?.putString(key, value) ?: run { inMemory[key] = value }
    }

    private fun rawRemove(key: String) {
        prefs?.remove(key) ?: inMemory.remove(key)
    }

    private fun rawAllKeys(): Set<String> =
        prefs?.allKeys() ?: inMemory.keys.toSet()

    private fun rawClear() {
        prefs?.clear() ?: inMemory.clear()
    }

    // ---- Flag evaluations ----

    /**
     * Persists one flag evaluation for a user.
     */
    fun saveFlag(userId: String, result: EvalResult) {
        rawPut(storageKey(FLAG_PREFIX, userId, result.key), result.toJson().toString())
    }

    /**
     * Retrieves the last successful evaluation of a flag for a user.
     * Returns null if not found or corrupt.
     */
    fun loadFlag(userId: String, flagKey: String): EvalResult? {
        val raw = rawGet(storageKey(FLAG_PREFIX, userId, flagKey)) ?: return null
        return try {
            EvalResult.fromJson(JSONObject(raw))
        } catch (e: Exception) {
            null
        }
    }

    /**
     * Removes a flag evaluation from storage.
     */
    fun removeFlag(userId: String, flagKey: String) {
        rawRemove(storageKey(FLAG_PREFIX, userId, flagKey))
    }

    // ---- Experiment assignments ----

    /**
     * Persists one experiment assignment for a user.
     */
    fun saveAssignment(userId: String, assignment: Assignment) {
        rawPut(storageKey(ASSIGNMENT_PREFIX, userId, assignment.experimentKey), assignment.toJson().toString())
    }

    /**
     * Retrieves the last successful assignment of a user to an experiment.
     * Returns null if not found or corrupt.
     */
    fun loadAssignment(userId: String, experimentKey: String): Assignment? {
        val raw = rawGet(storageKey(ASSIGNMENT_PREFIX, userId, experimentKey)) ?: return null
        return try {
            Assignment.fromJson(JSONObject(raw))
        } catch (e: Exception) {
            null
        }
    }

    /**
     * Removes an assignment from storage.
     */
    fun removeAssignment(userId: String, experimentKey: String) {
        rawRemove(storageKey(ASSIGNMENT_PREFIX, userId, experimentKey))
    }

    // ---- Maintenance ----

    /**
     * Number of stored results (flags + assignments).
     */
    fun size(): Int = rawAllKeys().count { it.startsWith(FLAG_PREFIX) || it.startsWith(ASSIGNMENT_PREFIX) }

    /**
     * Clears all stored results.
     */
    fun clearAll() {
        rawClear()
    }

    /**
     * Storage key `<prefix><len(userId)>_<userId>_<key>`. The length prefix keeps
     * user IDs and keys containing `_` unambiguous, and the key stays XML-safe for
     * SharedPreferences.
     */
    private fun storageKey(prefix: String, userId: String, key: String): String =
        "$prefix${userId.length}_${userId}_$key"

    companion object {
        private const val FLAG_PREFIX = "ep_flag_"
        private const val ASSIGNMENT_PREFIX = "ep_assign_"
    }
}
