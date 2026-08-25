import unittest

from query_understanding import build_query_plan


class QueryUnderstandingTests(unittest.TestCase):
    def _plan(self, text, norm=None, **kwargs):
        return build_query_plan(
            raw_text=text,
            norm_text=norm or text,
            brand=kwargs.pop("brand", "zeo"),
            query_entities=kwargs.pop("query_entities", {}),
            reference_resolution=kwargs.pop("reference_resolution", {}),
            conversation_state=kwargs.pop("conversation_state", {}),
        )

    def test_price_abbreviation_is_price_attribute(self):
        plan = self._plan("giá bn", "gia bao nhieu")
        self.assertEqual(plan.intent, "product_price_query")
        self.assertIn("price", plan.attributes)

    def test_availability_short_question(self):
        plan = self._plan("còn hàng k", "con hang khong")
        self.assertEqual(plan.intent, "product_availability_query")
        self.assertIn("availability", plan.attributes)

    def test_link_followup_resolves_product_reference(self):
        plan = self._plan(
            "xin link sản phẩm đó",
            "xin link san pham do",
            reference_resolution={
                "references_previous_turn": True,
                "resolved": True,
                "product": "Combo 4 nước giặt Pano",
                "product_id": "43672853910",
            },
        )
        self.assertEqual(plan.intent, "product_link_query")
        self.assertTrue(plan.references["resolved"])
        self.assertEqual(plan.references["product_id"], "43672853910")
        self.assertIn("link", plan.attributes)

    def test_ordinal_price_followup_needs_context_when_unresolved(self):
        plan = self._plan("cái đầu tiên giá nhiu", "cai dau tien gia nhieu")
        self.assertEqual(plan.references["ordinal"], 1)
        self.assertTrue(plan.needs_context)
        self.assertEqual(plan.ambiguity_reason, "UNRESOLVED_REFERENCE")

    def test_toilet_limescale_is_not_laundry_stain(self):
        plan = self._plan(
            "Bồn cầu bị cặn vôi ố vàng",
            "bon cau bi can voi o vang",
        )
        self.assertEqual(plan.intent, "cleaning_toilet_stain")
        self.assertEqual(plan.entities["category"], "toilet_cleaner")

    def test_front_load_washer_is_compatibility(self):
        plan = self._plan(
            "Nước giặt PANO 3.5kg có bị trào bọt không?",
            "nuoc giat pano 3.5kg co bi trao bot khong",
        )
        self.assertEqual(plan.intent, "product_compatibility")
        self.assertIn("compatibility", plan.attributes)
        self.assertEqual(plan.entities["variant"], "3.5kg")

    def test_multi_attribute_product_query(self):
        plan = self._plan(
            "loại này giá bao nhiêu, còn hàng không và có link mua không",
            "loai nay gia bao nhieu con hang khong va co link mua khong",
            reference_resolution={"references_previous_turn": True, "resolved": True, "product": "Nước rửa chén PANO"},
        )
        self.assertEqual(plan.intent, "multi_attribute_product_query")
        self.assertEqual(set(plan.attributes), {"price", "availability", "link"})

    def test_brand_ecosystem_question(self):
        plan = self._plan(
            "Bên mình có ZeO, PANO với Oplus là 3 hãng khác nhau hay sao?",
            "ben minh co zeo pano voi oplus la 3 hang khac nhau hay sao",
        )
        self.assertEqual(plan.intent, "brand_ecosystem_overview")

    def test_cskh_phone_request_is_company_contact(self):
        plan = self._plan(
            "Cho số chăm sóc kh cty ZeO Cần Thơ",
            "cho so cham soc khach hang cong ty zeo can tho",
        )
        self.assertEqual(plan.intent, "company_contact_information")
        self.assertFalse(plan.needs_product_tool)

    def test_constraints_capture_quantity_budget_channel_and_correction(self):
        plan = self._plan(
            "Không phải Pano, ý mình là ZeO, lấy 2 chai tầm 200k trên Shopee",
            "khong phai pano y minh la zeo lay 2 chai tam 200k tren shopee",
        )
        self.assertEqual(plan.constraints["quantity"], 2)
        self.assertEqual(plan.constraints["quantity_unit"], "chai")
        self.assertEqual(plan.constraints["budget_vnd"], 200_000)
        self.assertEqual(plan.constraints["channels"], ["shopee"])
        self.assertEqual(plan.constraints["negated_brands"], ["pano"])
        self.assertEqual(plan.constraints["corrected_brand"], "zeo")

    def test_cfc_dealer_location_is_not_profile_lookup(self):
        plan = self._plan(
            "Ở khu vực tôi có đại lý không",
            "o khu vuc toi co dai ly khong",
            brand="cfc",
        )
        self.assertEqual(plan.intent, "cfc_dealer_location_request")
        self.assertNotIn("location", plan.constraints)


if __name__ == "__main__":
    unittest.main()
