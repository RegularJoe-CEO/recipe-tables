"""
Pre-loaded example recipes — structured so the TRN cascade matches
classic Cooking for Engineers cards (see espresso-brownie reference).
"""

from __future__ import annotations

from .trn_converter import convert_text_to_trn
from .models import TRNTable

# Closely mirrors the classic CFE espresso / fudge brownie summary table
CLASSIC_BROWNIES = """
Classic Fudge Brownies

Servings: 16 squares

Ingredients
- 4 oz unsalted butter
- 1 cup granulated sugar
- 1/4 tsp vanilla extract
- 1 shot fresh brewed espresso or very strong coffee
- 2 large eggs
- 1/2 cup all-purpose flour
- 1/3 cup cocoa powder
- 1/4 tsp baking soda
- 1/4 tsp table salt

Instructions
1. Butter and flour an 8x8-in pan.
2. Preheat oven to 350°F (170°C).
3. Melt the unsalted butter.
4. Mix in the granulated sugar, vanilla extract, and espresso.
5. Mix in the eggs.
6. Fold in the all-purpose flour, cocoa powder, baking soda, and table salt.
7. Bake at 350°F (170°C) for 30 to 40 minutes.
"""

CHICKEN_STIR_FRY = """
Garlic Ginger Chicken Stir-Fry

Servings: 4

Ingredients
- 1 lb boneless chicken breast, sliced thin
- 2 tbsp soy sauce
- 1 tbsp cornstarch
- 2 tbsp vegetable oil
- 3 cloves garlic, minced
- 1 tbsp fresh ginger, grated
- 1 red bell pepper, sliced
- 2 cups broccoli florets
- 3 tbsp oyster sauce
- 1 tsp sesame oil
- 2 green onions, sliced
- cooked rice, for serving

Instructions
1. In a bowl, toss the chicken with soy sauce and cornstarch. Set aside for 10 minutes.
2. Heat 1 tablespoon vegetable oil in a large skillet or wok over high heat.
3. Stir-fry the chicken until just cooked through, 4–5 minutes. Transfer to a plate.
4. Add remaining oil to the pan. Sauté garlic and ginger for 30 seconds until fragrant.
5. Add the red bell pepper and broccoli florets; stir-fry 3–4 minutes until crisp-tender.
6. Return the chicken to the pan. Add oyster sauce and sesame oil; toss to coat.
7. Garnish with green onions and serve over cooked rice.
"""

TOMATO_BASIL_PASTA = """
Simple Tomato Basil Pasta

Servings: 4

Ingredients
- 12 oz spaghetti
- 3 tbsp olive oil
- 4 cloves garlic, thinly sliced
- 28 oz crushed tomatoes
- 1/2 tsp red pepper flakes
- 1 tsp salt
- 1/2 tsp black pepper
- 1 cup fresh basil leaves, torn
- 1/2 cup grated Parmesan cheese

Instructions
1. Bring a large pot of salted water to a boil. Cook the spaghetti until al dente according to package directions. Reserve 1/2 cup pasta water, then drain.
2. Meanwhile, heat the olive oil in a large skillet over medium heat. Add the garlic and cook until golden, about 1–2 minutes.
3. Stir in the crushed tomatoes, red pepper flakes, salt, and black pepper. Simmer 10 minutes.
4. Add the drained spaghetti to the sauce with a splash of pasta water. Toss to coat.
5. Remove from heat. Fold in the fresh basil and half the Parmesan.
6. Serve topped with remaining Parmesan cheese.
"""

EXAMPLES: dict[str, str] = {
    "Classic Fudge Brownies": CLASSIC_BROWNIES,
    "Garlic Ginger Chicken Stir-Fry": CHICKEN_STIR_FRY,
    "Simple Tomato Basil Pasta": TOMATO_BASIL_PASTA,
}


def get_example(name: str) -> TRNTable:
    text = EXAMPLES[name]
    return convert_text_to_trn(text, title=name, source=f"example: {name}")


def example_names() -> list[str]:
    return list(EXAMPLES.keys())
