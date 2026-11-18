/**
 * Generic utility functions
 */

/**
 * Sort a list by name property
 * @param {Array} list - List of objects with name property
 * @returns {Array} Sorted copy of the list
 */
export function sortByName(list) {
  return [...list].sort((a, b) => (a.name || "").localeCompare(b.name || ""));
}
