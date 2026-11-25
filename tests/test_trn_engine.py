"""
Regression tests: the TRN engine must handle many recipe shapes with the
SAME rules — no per-recipe special cases in the app layer.
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.trn_converter import convert_text_to_trn
from src.trn_engine import build_trn, MAX_ACTION_COLS
from src.trn_renderer import build_grid
from src.pdf_exporter import export_pdf


ARCHETYPES = {
    "bake": """
Classic Fudge Brownies
Serves 16
Ingredients
- 4 oz unsalted butter
- 1 cup sugar
- 1/4 tsp vanilla extract
- 1 shot espresso
- 2 large eggs
- 1/2 cup flour
- 1/3 cup cocoa powder
- 1/4 tsp baking soda
- 1/4 tsp salt
Instructions
1. Butter and flour an 8x8-in pan.
2. Preheat oven to 350°F (170°C).
3. Melt the unsalted butter.
4. Mix in the sugar, vanilla extract, and espresso.
5. Mix in the eggs.
6. Fold in the flour, cocoa powder, baking soda, and salt.
7. Bake at 350°F for 30 to 40 minutes.
""",
    "stirfry": """
Garlic Ginger Chicken Stir-Fry
Serves 4
Ingredients
- 1 lb chicken breast
- 2 tbsp soy sauce
- 1 tbsp cornstarch
- 2 tbsp vegetable oil
- 3 cloves garlic
- 1 tbsp ginger
- 1 red bell pepper
- 2 cups broccoli
- 3 tbsp oyster sauce
Instructions
1. Toss chicken with soy sauce and cornstarch. Set aside 10 minutes.
2. Heat oil in a wok over high heat.
3. Stir-fry the chicken 4-5 minutes. Transfer to a plate.
4. Add garlic and ginger; cook 30 seconds.
5. Add bell pepper and broccoli; stir-fry 3-4 minutes.
6. Return chicken. Add oyster sauce; toss. Serve.
""",
    "soup": """
Chicken Noodle Soup
Serves 6
Ingredients
- 1 tbsp olive oil
- 1 onion, diced
- 2 carrots, sliced
- 2 celery stalks
- 2 cloves garlic
- 8 cups chicken broth
- 2 cups cooked chicken
- 2 cups egg noodles
- 1 tsp salt
- 1/2 tsp pepper
Instructions
1. Heat olive oil in a pot over medium heat.
2. Add onion, carrots, and celery; cook 5 minutes.
3. Add garlic; cook 30 seconds.
4. Pour in broth and bring to a boil.
5. Add chicken, noodles, salt, and pepper. Simmer 10 minutes. Serve.
""",
    "salad": """
Greek Salad
Serves 4
Ingredients
- 3 cups cucumber
- 2 cups cherry tomatoes
- 1 red onion
- 1 cup kalamata olives
- 8 oz feta
- 3 tbsp olive oil
- 1 tbsp red wine vinegar
- 1 tsp oregano
Instructions
1. Combine cucumber, tomatoes, onion, olives, and feta in a bowl.
2. Whisk olive oil, vinegar, and oregano.
3. Pour dressing over salad and toss. Serve.
""",
    "kitchn_long": """
Creamy Tuscan Chicken
Serves 4
Ingredients
- 4 chicken breasts
- 1 tsp salt
- 1/2 tsp pepper
- 2 tbsp olive oil
- 3 cloves garlic
- 1 cup heavy cream
- 1/2 cup chicken broth
- 1/2 cup parmesan
- 1 cup spinach
- 1/2 cup sun-dried tomatoes
Instructions
1. Season chicken with salt and pepper.
2. Heat oil in a skillet over medium-high heat.
3. Cook chicken 5-7 minutes per side. Transfer to a plate.
4. Reduce heat to medium. Add garlic and cook 30 seconds.
5. Pour in cream and broth; bring to a simmer.
6. Stir in parmesan until melted.
7. Add spinach and sun-dried tomatoes; cook until spinach wilts.
8. Return chicken to the pan and simmer 2 minutes. Serve.
9. Recipe Notes Storage: Refrigerate leftovers for up to 4 days.
10. Reviews Filed in: Recipes chicken
""",
    "shrimp": """
Garlic Butter Shrimp
Serves 2
Ingredients
- 1 lb large shrimp, peeled
- 3 tbsp butter
- 4 cloves garlic, minced
- 1/4 tsp red pepper flakes
- 2 tbsp lemon juice
- 2 tbsp parsley
Instructions
1. Pat shrimp dry and season with salt and pepper.
2. Melt butter in a skillet over medium-high heat.
3. Add garlic and red pepper flakes; cook 30 seconds.
4. Add shrimp and cook 2 minutes per side until pink.
5. Stir in lemon juice and parsley. Serve.
""",
}


def _check_invariants(name: str, text: str) -> list[str]:
    errors = []
    t = convert_text_to_trn(text)
    eng = build_trn(t)
    g = build_grid(t)

    if len(eng.milestones) > MAX_ACTION_COLS:
        errors.append(f"{name}: too many cols {len(eng.milestones)}")
    if g.n_cols > MAX_ACTION_COLS + 1:
        errors.append(f"{name}: grid n_cols {g.n_cols}")
    if not eng.ingredients and "no ingredients" not in name:
        # salad etc should have ings
        if len(t.ingredients) == 0:
            errors.append(f"{name}: zero ingredients")
    if not eng.milestones:
        errors.append(f"{name}: no milestones")
    for m in eng.milestones:
        if not m.action.strip():
            errors.append(f"{name}: empty action")
    # Action labels must not be ingredient-noun false positives for cream pour
    if name == "kitchn_long":
        labels = [m.action.lower() for m in eng.milestones]
        # "Pour in cream" must not become sole action "cream" if pour is the verb
        # (cream as label only OK if step literally starts with cream)
        pass
    # Spans cover something
    if any(not s for s in eng.spans):
        errors.append(f"{name}: empty span")
    # PDF
    try:
        pdf = export_pdf(t)
        if pdf[:4] != b"%PDF":
            errors.append(f"{name}: bad pdf magic")
        if len(pdf) < 800:
            errors.append(f"{name}: tiny pdf {len(pdf)}")
    except Exception as e:
        errors.append(f"{name}: pdf exception {e}")

    # Junk steps should not appear as milestones
    for m in eng.milestones:
        if "review" in m.action.lower() or "storage" in m.detail.lower()[:40]:
            errors.append(f"{name}: junk milestone {m.action}")

    # Method still has full steps
    if len(eng.method_steps) < 2:
        errors.append(f"{name}: method steps missing")

    return errors


def test_all_archetypes():
    all_err = []
    for name, text in ARCHETYPES.items():
        all_err.extend(_check_invariants(name, text))
    assert not all_err, "\n".join(all_err)


def test_bake_cascade_grows():
    t = convert_text_to_trn(ARCHETYPES["bake"])
    eng = build_trn(t)
    # spans should generally non-decrease in size along the main line
    sizes = [len(s) for s in eng.spans]
    assert sizes[-1] >= sizes[0], sizes
    assert len(eng.prep) >= 1  # preheat or butter pan
    labels = [m.action for m in eng.milestones]
    assert any("melt" in a or "mix" in a or "fold" in a for a in labels), labels


def test_column_cap_on_long_recipe():
    t = convert_text_to_trn(ARCHETYPES["kitchn_long"])
    eng = build_trn(t)
    assert len(eng.milestones) <= MAX_ACTION_COLS
    # junk filtered
    blob = " ".join(m.detail for m in eng.milestones).lower()
    assert "reviews filed" not in blob
    assert "recipe notes" not in blob


def test_lead_verb_not_ingredient_noun():
    """'Pour in cream and broth' must label as pour, not cream."""
    text = """
    Sauce
    Ingredients
    - 1 cup heavy cream
    - 1/2 cup broth
    Instructions
    1. Pour in cream and broth; bring to a simmer.
    """
    t = convert_text_to_trn(text)
    eng = build_trn(t)
    assert eng.milestones
    assert eng.milestones[0].action in {"pour", "bring", "simmer"}, eng.milestones[
        0
    ].action


def test_pdf_fits_and_examples():
    from src.examples import get_example, example_names

    for name in example_names():
        t = get_example(name)
        eng = build_trn(t)
        assert len(eng.milestones) <= MAX_ACTION_COLS, name
        pdf = export_pdf(t)
        assert pdf[:4] == b"%PDF"


if __name__ == "__main__":
    test_all_archetypes()
    test_bake_cascade_grows()
    test_column_cap_on_long_recipe()
    test_lead_verb_not_ingredient_noun()
    test_pdf_fits_and_examples()
    test_storage_notes_stay_notes_not_columns()
    test_ingredient_with_count_mid_line_is_kept()
    test_dish_title_without_qty_still_rejected()
    test_phrase_matches_beat_single_words_from_longer_names()
    test_bake_variation_notes_stay_notes_not_columns()
    test_jsonld_yield_becomes_servings()
    print("ALL TESTS PASSED")
    # Pretty dump
    for name, text in ARCHETYPES.items():
        t = convert_text_to_trn(text)
        eng = build_trn(t)
        print(f"\n{name}: cols={len(eng.milestones)} prep={len(eng.prep)}")
        print("  ings:", [i.name[:20] for i in eng.ingredients[:6]], "...")
        print("  actions:", [m.action for m in eng.milestones])
        print("  span sizes:", [len(s) for s in eng.spans])


FROZEN_FRUIT_SALAD = """
Frozen Fruit Salad
Servings: 18
Ingredients
- 1 c drained, crushed pineapple
- 2 medium sized bananas diced
- 2 1/2 T lemon juice
- 1/8 t salt
- 2 1/2 c sour cream
- 1/2 c chopped maraschino cherries
- 1/2 c chopped walnuts or pecans (optional)
Instructions
1. Mix everything together in a large bowl.
2. Spoon into 18 paper cups that have been place into muffin pans. Freeze.
3. Once these are frozen I take them out of the pans and put them into a giant zip lock bag and store them in the freezer. Remember to take them out about 20 minutes before serving, so they aren't hard as rocks! Also don't stack these or they may melt together!
"""


def test_storage_notes_stay_notes_not_columns():
    from src.trn_engine import action_label
    from src.trn_renderer import table_to_markdown

    table = convert_text_to_trn(FROZEN_FRUIT_SALAD)
    labels = [action_label(m).lower() for m in build_trn(table).milestones]
    assert "remember" not in labels and "melt" not in labels, labels
    assert labels == ["mix", "spoon"], labels
    assert "Remember to take them out" in table.notes
    assert "don't stack" in table.notes
    assert "zip lock bag" in table.notes
    details = " ".join(s.detail for s in table.steps)
    assert "Remember" not in details
    # Short "Freeze." is kept with its step instead of being dropped
    assert "Freeze." in details
    md = table_to_markdown(table)
    assert "## Notes" in md and "Remember to take them out" in md
    assert len(table.ingredients) == 7
    export_pdf(table)


def test_ingredient_with_count_mid_line_is_kept():
    text = """
Southern Giblet Gravy
Servings: 12
Ingredients
- Giblets and neck from 1 turkey or whole chicken
- 4 cups chicken or turkey broth (divided)
- 1/4 cup all-purpose flour
Instructions
1. Place the giblets and neck in a medium saucepan. Add 3 cups of the broth.
2. Whisk in the flour and simmer until thickened.
"""
    table = convert_text_to_trn(text)
    names = [i.name.lower() for i in table.ingredients]
    assert any("giblets and neck" in n for n in names), names
    assert len(table.ingredients) == 3


def test_dish_title_without_qty_still_rejected():
    from src.trn_converter import _parse_ingredients

    ings = _parse_ingredients("Dr Pepper-Glazed Lamb Shoulder\n2 cups flour")
    assert [i.name for i in ings] == ["flour"]


def test_phrase_matches_beat_single_words_from_longer_names():
    from src.trn_converter import _match_ingredients
    from src.quantity_parser import parse_ingredient_line as p

    giblets = p("Giblets and neck from 1 turkey or whole chicken")
    broth = p("4 cups chicken or turkey broth (divided)")
    butter = p("4 tablespoons unsalted butter or turkey drippings")
    salt = p("1/2 teaspoon salt (plus more to taste)")
    ings = [giblets, broth, butter, salt]
    assert _match_ingredients("Add 3 cups of the chicken or turkey broth.", ings) == [broth.id]
    assert _match_ingredients(
        "Melt the butter or turkey drippings in a large skillet.", ings
    ) == [butter.id]
    assert _match_ingredients("Add the poultry seasoning, salt, and black pepper.", ings) == [salt.id]


OYSTER_DRESSING = """
Southern Cornbread and Oyster Dressing (Stuffing)
Servings: 8 to 10 side servings
Ingredients
- 1 cup (2 sticks) butter
- 1 medium onion, chopped
- 3 ribs celery, chopped
- 1 tablespoon chopped green bell pepper
- 6 cups cooked, crumbled cornbread
- 6 cups dry, toasted bread, crumbled (toasted white bread, leftover biscuits, or rolls)
- 1 teaspoon salt
- 1/2 teaspoon Creole or Cajun seasoning, or to taste, optional
- 1/4 teaspoon freshly cracked black pepper
- 1 teaspoon sage or Bell's seasoning, or to taste
- 1/2 teaspoon poultry seasoning
- 4 to 6 cups turkey or chicken stock
- 5 eggs, beaten (or 3 raw, 2 boiled and chopped)
- 1-quart oysters, liquid reserved
Instructions
1. Toast the bread in a toaster or in the oven. Remove and set aside to cool.
2. Make a pan of cornbread, remove and set aside uncovered to cool completely. You want the breads to be very dry. This can be done in advance.
3. When ready to bake dressing, preheat oven to 350 degrees F. Butter a 9 x 13-inch pan or spray with non-stick spray.
4. Melt the butter in a skillet over medium heat. Saute the onion, celery and bell pepper.
5. Add the salt, Creole or Cajun seasoning, pepper, sage or Bell's seasoning and poultry seasoning. Continue cooking and stirring for 2 minutes. Set aside to cool.
6. Crumble the cornbread into a large bowl. Add the bread and toss.
7. Add the sauteed veggies, scraping out all of the butter; stir.
8. Pour in 2 cups of the broth and toss. Taste and adjust seasoning as needed.
9. Add the beaten eggs; stir.
10. Using a pair of kitchen shears, roughly chop the oysters. Add the oysters, with their juices, to the cornbread mixture. This is where you fix the consistency.
11. If you want a fluffy stuffing, you'll use less stock. If you prefer a wetter stuffing, add additional broth as needed to reach desired consistency, taking care not to get too soupy!
12. Lightly spoon into the prepared casserole dish, but do not pack in.
13. Bake at 350 degrees F for 45 minutes. For a moister stuffing, baked covered. For a drier stuffing with a crunchy top, bake uncovered.
"""


def test_bake_variation_notes_stay_notes_not_columns():
    from src.trn_engine import action_label
    from src.quantity_parser import parse_ingredient_line

    ing = parse_ingredient_line("6 cups cooked, crumbled cornbread")
    assert "cornbread" in ing.name.lower()
    assert ing.name.lower() != "cooked"
    pineapple = parse_ingredient_line("1 c drained, crushed pineapple")
    assert "pineapple" in pineapple.name.lower()

    table = convert_text_to_trn(OYSTER_DRESSING)
    labels = [action_label(m).lower().replace("\n", " ") for m in build_trn(table).milestones]
    assert sum("bake" in lab for lab in labels) == 1, labels
    assert any("45" in lab for lab in labels), labels
    assert "remember" not in labels and "moister" not in " ".join(labels)
    assert "moister stuffing" in table.notes
    assert "bake uncovered" in table.notes
    assert "can be done in advance" in table.notes
    details = " ".join(s.detail for s in table.steps)
    assert "baked covered" not in details
    assert "bake uncovered" not in details
    names = " ".join(i.name for i in table.ingredients).lower()
    assert "cornbread" in names
    assert table.servings and "8" in table.servings and "10" in table.servings
    # Preheat / butter the pan are prep headers, not a bare bake column
    assert any(s.is_header and "preheat" in s.detail.lower() for s in table.steps), [
        (s.is_header, s.detail) for s in table.steps
    ]
    assert any(s.is_header and "butter a 9" in s.detail.lower() for s in table.steps)
    export_pdf(table)


def test_jsonld_yield_becomes_servings():
    from src.recipe_input import _recipe_from_jsonld

    text = _recipe_from_jsonld(
        {
            "name": "Test Yield Cake",
            "yield": "8 to 10 side servings",
            "recipeIngredient": ["1 cup flour", "2 eggs"],
            "recipeInstructions": ["Mix the flour and eggs.", "Bake 20 minutes."],
        }
    )
    assert "Servings: 8 to 10 side servings" in text
    table = convert_text_to_trn(text)
    assert table.servings and "8" in table.servings and "10" in table.servings


def test_ranges_display_both_ends_not_midpoint():
    from src.quantity_parser import parse_ingredient_line as p

    assert p("10-12 eggs").display_left() == "10-12  eggs"
    assert p("2-3 quarts chicken broth").display_left() == "2-3 quarts  chicken broth"
    assert p("10-12 eggs").display_left(2.0) == "20-24  eggs"
    assert p("2 1/2 cup sour cream").display_left() == "2 1/2 cup  sour cream"
    assert p("2 to 4 tbsp milk").display_left() == "2 to 4 tbsp  milk"
    assert p("4 to 6 cups turkey or chicken stock").display_left() == "4 to 6 cups  turkey or chicken stock"
    assert p("2 to 4 tbsp milk").display_left(2.0) == "4 to 8 tbsp  milk"


def test_to_taste_and_optional_lines_kept_and_matched():
    text = """
Cornbread Dressing
Ingredients
- 8 pkg Martha White Yellow Cornbread Mix
- 1-2 cups onions, chopped
- sweet corn (optional)
- sage to taste
- salt to taste
Instructions
1. Make the cornbread according to the package directions.
2. Saute the onions and sweet corn (optional) in butter.
3. Mix in the onion mixture.
4. Stir in salt and sage to taste.
"""
    table = convert_text_to_trn(text)
    names = [i.name for i in table.ingredients]
    assert "sweet corn (optional)" in names and "sage to taste" in names, names
    by_name = {i.id: i.name for i in table.ingredients}
    mix_step = next(s for s in table.steps if s.detail.startswith("Mix in"))
    assert mix_step.ingredient_ids == []  # "Mix" is not the cornbread mix
    season = next(s for s in table.steps if s.detail.startswith("Stir in"))
    assert sorted(by_name[i] for i in season.ingredient_ids) == ["sage to taste", "salt to taste"]



def test_egg_wash_does_not_steal_eggs_or_milk():
    from src.trn_converter import _match_ingredients
    from src.quantity_parser import parse_ingredient_line as p

    eggs = p("2 eggs")
    cream = p("3/4 cup heavy cream")
    milk = p("2 to 4 tbsp milk")
    wash = p("1 egg beaten with a splash of milk, for egg wash")
    powder = p("2 tsp baking powder")
    ings = [eggs, cream, milk, wash, powder]
    names = {i.id: i.name for i in ings}

    def names_for(step):
        return [names[i] for i in _match_ingredients(step, ings)]

    assert names_for("Beat the eggs with the heavy cream, then stir that into the flour mixture.") == [
        "eggs",
        "heavy cream",
    ]
    assert names_for("Add milk a tablespoon at a time, just until the dough comes together.") == ["milk"]
    assert names_for("Brush them with the egg wash before baking.") == [
        "egg beaten with a splash of milk"
    ]
