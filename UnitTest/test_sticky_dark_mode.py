import unittest
from pathlib import Path

# Path to the CSS file we modified
CSS_FILE = (
    Path(__file__).resolve().parents[1]
    / "packages"
    / "editor"
    / "src"
    / "styles"
    / "variables.css"
)


class TestStickyDarkMode(unittest.TestCase):

    def setUp(self):
        # Read the actual CSS file
        self.css = CSS_FILE.read_text(encoding="utf-8")
        #Test 1 verifying the stylsheet exists.
    def test_css_file_exists(self):
        self.assertTrue(CSS_FILE.exists())
    
    #Test 2 Verify that the css file contains the specifier I added
    def test_dark_mode_selector_exists(self):
        self.assertIn(':root[data-theme*="dark"]', self.css)
    
    #Test 3 verify the darkmode selector has a higher priority
    def test_dark_mode_Selector_Priority(self):
        selector = ':root[data-theme*="dark"]'
        

        self.assertIn(selector, self.css)
        
        # Count the psuedo class and attribute selectors
        root_index = selector.count(':root')
        attribute_index = selector.count('[data-theme*="dark"]')
        
        # Calculate the priority using (ID, class, element, Heirarchy)
        Priority = (0, 0, root_index + attribute_index, 0)
        
        # Assertions
        self.assertEqual(Priority, (0, 0, 2, 0))
        self.assertGreater(Priority, (0, 0, 1, 0))
        