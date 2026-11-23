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
