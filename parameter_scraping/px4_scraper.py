import logging
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, Optional, List
from pathlib import Path

@dataclass
class ParameterMetadata:
    name: str
    type: str
    default: str
    short_desc: str
    long_desc: str
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    unit: Optional[str] = None
    decimal: Optional[int] = None
    group: str = "Uncategorized"
    values: Dict[str, str] = field(default_factory=dict)  # For enum parameters
    volatile: bool = False
    category: str = "Standard"
    source_file: Optional[str] = None  # Track which file defined the parameter
    line_number: Optional[int] = None  # Track line number of definition

class PX4ParameterParser:
    def __init__(self, repo_path: Path, output_dir: Optional[Path] = None):
        self.repo_path = repo_path
        # If no output_dir specified, create 'scraped_data' in the same directory as the script
        if output_dir is None:
            self.output_dir = Path(__file__).parent / "scraped_data"
        else:
            self.output_dir = output_dir
        self.parameters: Dict[str, ParameterMetadata] = {}
        
        # Configure logging
        logging.basicConfig(level=logging.DEBUG,
                          format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s')
        self.logger = logging.getLogger(__name__)
        
        # Compile regex patterns for source parsing
        self.re_comment_start = re.compile(r'\/\*\*')
        self.re_comment_content = re.compile(r'\*\s*(.*)')
        self.re_comment_tag = re.compile(r'@([a-zA-Z][a-zA-Z0-9_]*)\s*(.*)')
        self.re_comment_end = re.compile(r'(.*?)\s*\*\/')
        self.re_param_define = re.compile(r'PARAM_DEFINE_([A-Z_][A-Z0-9_]*)\s*\(([A-Z_][A-Z0-9_]*)\s*,\s*([^ ,\)]+)\s*\)\s*;')
        self.re_px4_param_define = re.compile(r'PX4_PARAM_DEFINE_([A-Z_][A-Z0-9_]*)\s*\(([A-Z_][A-Z0-9_]*)\s*\)\s*;')
        
        self.ensure_output_dir()

    def ensure_output_dir(self):
        """Ensure the output directory exists."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.logger.info(f"Output directory set to: {self.output_dir}")
        
    def parse_all(self):
        """Parse both injected and source parameters"""
        self.logger.info("Starting parameter parsing")
        
        # Parse the injected parameters first
        injected_path = self.repo_path / "src/lib/parameters/parameters_injected.xml"
        if injected_path.exists():
            self.logger.info(f"Parsing injected parameters from {injected_path}")
            self.parse_xml_file(injected_path)
            
        # Parse parameters from source files
        self.logger.info("Scanning source files for parameters")
        for ext in ['.c', '.cpp', '.h', '.hpp']:
            for src_file in self.repo_path.rglob(f"*{ext}"):
                if 'build' not in str(src_file):  # Skip build directory
                    self.parse_source_file(src_file)
            
        # Save the parsed parameters
        self.save_parameters()
            
    def parse_xml_file(self, xml_path: Path):
        """Parse a single XML file containing parameter definitions."""
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
            
            for group in root.findall('group'):
                group_name = group.attrib.get('name', 'Uncategorized')
                
                for param in group.findall('parameter'):
                    metadata = self._parse_parameter(param, group_name)
                    if metadata:
                        self.parameters[metadata.name] = metadata
                        self.logger.debug(f"Parsed parameter from XML: {metadata.name}")
        except Exception as e:
            self.logger.error(f"Error parsing {xml_path}: {str(e)}")

    def parse_source_file(self, src_path: Path):
        """Parse parameters from a source file."""
        try:
            with open(src_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # Find all parameter definitions
            current_comment = []
            in_comment = False
            line_number = 0
            
            for line in content.split('\n'):
                line_number += 1
                line = line.strip()
                
                # Handle comment blocks
                if self.re_comment_start.search(line):
                    in_comment = True
                    current_comment = []
                elif in_comment:
                    if '*/' in line:
                        in_comment = False
                        current_comment.append(line)
                    else:
                        current_comment.append(line)
                elif not in_comment:
                    # Look for parameter definitions
                    param_match = self.re_param_define.search(line)
                    if param_match:
                        param_type, param_name, param_default = param_match.groups()
                        self._process_parameter_definition(
                            param_type, 
                            param_name, 
                            param_default, 
                            current_comment,
                            src_path.relative_to(self.repo_path),
                            line_number
                        )
                        current_comment = []
                        
        except Exception as e:
            self.logger.error(f"Error parsing source file {src_path}: {str(e)}")

    def _process_parameter_definition(self, param_type: str, param_name: str, 
                                    param_default: str, comment_lines: List[str],
                                    source_file: Path, line_number: int):
        """Process a parameter definition found in source code."""
        try:
            # Parse comment block
            short_desc = ""
            long_desc = ""
            metadata = {}
            
            for line in comment_lines:
                line = line.strip('/* ')
                
                # Look for @tags
                tag_match = self.re_comment_tag.search(line)
                if tag_match:
                    tag, value = tag_match.groups()
                    metadata[tag] = value
                elif not short_desc:
                    short_desc = line
                else:
                    long_desc += line + " "
            
            # Create parameter metadata
            param = ParameterMetadata(
                name=param_name,
                type=param_type,
                default=param_default,
                short_desc=short_desc.strip(),
                long_desc=long_desc.strip(),
                group=metadata.get('group', 'Uncategorized'),
                source_file=str(source_file),
                line_number=line_number
            )
            
            # Add optional fields
            if 'min' in metadata:
                param.min_val = float(metadata['min'])
            if 'max' in metadata:
                param.max_val = float(metadata['max'])
            if 'unit' in metadata:
                param.unit = metadata['unit']
            if 'decimal' in metadata:
                param.decimal = int(metadata['decimal'])
            if 'volatile' in metadata:
                param.volatile = True
            if 'category' in metadata:
                param.category = metadata['category']
                
            self.parameters[param_name] = param
            self.logger.debug(f"Parsed parameter from source: {param_name}")
            
        except Exception as e:
            self.logger.error(f"Error processing parameter {param_name}: {str(e)}")
                    
    def _parse_parameter(self, param_elem: ET.Element, group: str) -> Optional[ParameterMetadata]:
        """Parse a single parameter element from XML."""
        try:
            name = param_elem.attrib.get('name')
            if not name:
                return None
                
            metadata = ParameterMetadata(
                name=name,
                type=param_elem.attrib.get('type', ''),
                default=param_elem.attrib.get('default', ''),
                short_desc=param_elem.find('short_desc').text if param_elem.find('short_desc') is not None else '',
                long_desc=param_elem.find('long_desc').text if param_elem.find('long_desc') is not None else '',
                group=group,
                volatile='volatile' in param_elem.attrib and param_elem.attrib['volatile'] == 'true'
            )
            
            # Parse optional fields
            if min_elem := param_elem.find('min'):
                metadata.min_val = float(min_elem.text)
            if max_elem := param_elem.find('max'):
                metadata.max_val = float(max_elem.text)
            if unit_elem := param_elem.find('unit'):
                metadata.unit = unit_elem.text
            if decimal_elem := param_elem.find('decimal'):
                metadata.decimal = int(decimal_elem.text)
                
            # Parse enum values if present
            values_elem = param_elem.find('values')
            if values_elem is not None:
                metadata.values = {
                    value.attrib['code']: value.text
                    for value in values_elem.findall('value')
                }
                
            return metadata
        except Exception as e:
            self.logger.error(f"Error parsing parameter {name if name else 'unknown'}: {str(e)}")
            return None

    def save_parameters(self):
        """Save the parsed parameters to a JSON file."""
        output_file = os.path.join(self.output_dir, "px4_parameters.json")
        
        # Convert parameters to dictionary format for JSON
        params_dict = {
            name: {
                "name": p.name,
                "type": p.type,
                "default": p.default,
                "short_desc": p.short_desc,
                "long_desc": p.long_desc,
                "min_val": p.min_val,
                "max_val": p.max_val,
                "unit": p.unit,
                "decimal": p.decimal,
                "group": p.group,
                "values": p.values,
                "volatile": p.volatile,
                "category": p.category,
                "source_file": p.source_file,
                "line_number": p.line_number
            }
            for name, p in self.parameters.items()
        }
        
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(params_dict, f, indent=2, ensure_ascii=False)
            self.logger.info(f"Successfully saved {len(self.parameters)} parameters to {output_file}")
        except Exception as e:
            self.logger.error(f"Error saving parameters to {output_file}: {str(e)}")

if __name__ == "__main__":
    # Get the path to the script's directory
    script_dir = Path(__file__).parent.resolve()
    # PX4-Autopilot is in the parent directory of the script
    px4_repo_path = script_dir.parent / "PX4-Autopilot"
    
    if not px4_repo_path.exists():
        print(f"Error: PX4-Autopilot repository not found at {px4_repo_path}")
        print("Please ensure you have cloned the PX4-Autopilot repository")
        exit(1)
        
    parser = PX4ParameterParser(px4_repo_path)
    parser.parse_all() 