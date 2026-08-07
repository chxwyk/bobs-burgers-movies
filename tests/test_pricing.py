import unittest

from movie_bot.pricing import (
    InputError,
    discounted_price_cents,
    format_cents,
    parse_money,
    parse_seats,
    validate_zip_code,
)


class PricingTests(unittest.TestCase):
    def test_order_total_range(self) -> None:
        self.assertEqual(parse_money("$40"), 4000)
        self.assertEqual(parse_money("86.42"), 8642)
        self.assertEqual(parse_money("250.00"), 25000)
        with self.assertRaises(InputError):
            parse_money("39.99")
        with self.assertRaises(InputError):
            parse_money("250.01")

    def test_exact_half_uses_currency_safe_rounding(self) -> None:
        self.assertEqual(discounted_price_cents(8642), 4321)
        self.assertEqual(discounted_price_cents(4001), 2001)

    def test_seats_and_zip_validation(self) -> None:
        self.assertEqual(parse_seats("3"), 3)
        self.assertEqual(validate_zip_code("89109"), "89109")
        with self.assertRaises(InputError):
            parse_seats("0")
        with self.assertRaises(InputError):
            validate_zip_code("ABC")

    def test_currency_format(self) -> None:
        self.assertEqual(format_cents(4321), "$43.21")


if __name__ == "__main__":
    unittest.main()
