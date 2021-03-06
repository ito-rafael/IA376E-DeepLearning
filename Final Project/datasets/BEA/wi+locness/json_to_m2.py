import argparse
import json
import os
import re
import spacy
from bisect import bisect
from operator import itemgetter
from string import punctuation
from nltk.stem.lancaster import LancasterStemmer
import scripts.align_text as align_text
import scripts.cat_rules as cat_rules
import scripts.toolbox as toolbox

# Convert Cambridge English Write & Improve (WI) (and new public FCE) JSON to ERRANT M2.
def main(args):
	# Get base working directory.
	basename = os.path.dirname(os.path.realpath(__file__))
	print("Loading resources...")
	# Punctuation normalisation dictionary
	norm_dict = {"’": "'",
				 "´": "'",
				 "‘": "'",
				 "′": "'",
				 "`": "'",
				 '“': '"',
				 '”': '"',
				 '˝': '"',
				 '¨': '"',
				 '„': '"',
				 '『': '"',
				 '』': '"',
				 '–': '-',
				 '—': '-',
				 '―': '-',
				 '¬': '-',
				 '、': ',',
				 '，': ',',
				 '：': ':',
				 '；': ';',
				 '？': '?',
				 '！': '!',
				 'ِ': ' ',
				 '\u200b': ' '}
	norm_dict = {ord(k): v for k, v in norm_dict.items()}
	# Load Tokenizer and other resources
	nlp = spacy.load("en")
	# Lancaster Stemmer
	stemmer = LancasterStemmer()
	# GB English word list (inc -ise and -ize)
	gb_spell = toolbox.loadDictionary(basename+"/resources/en_GB-large.txt")
	# Part of speech map file
	tag_map = toolbox.loadTagMap(basename+"/resources/en-ptb_map")
	# Setup output m2 file
	out_m2 = open(args.out, "w")

	print("Preprocessing files...")
	# Open the file
	with open(args.json_file) as data:
		# Process each line
		for line in data:
			# Load the JSON line
			line = json.loads(line)
			# Normalise certain punctuation in the text
			text = line["text"].translate(norm_dict)
			# Store the sentences and edits for all annotators here
			coder_dict = {}
			# Loop through the annotator ids and their edits
			for coder, edits in line["edits"]:
				# Add the coder to the coder_dict if needed
				if coder not in coder_dict: coder_dict[coder] = []
				# Split the essay into paragraphs and update and normalise the char edits
				para_info = getParas(text, edits, norm_dict)
				# Loop through the paragraphs and edits
				for orig_para, para_edits in para_info:
					# Remove unnecessary whitespace from para and update char edits
					orig_para, para_edits = cleanPara(orig_para, para_edits)
					if not orig_para: continue # Ignore empty paras
					# Annotate orig_para with spacy
					orig_para = nlp(orig_para)
					# Convert character edits to token edits
					para_edits = getTokenEdits(orig_para, para_edits, nlp)
					# Split the paragraph into sentences and update tok edits
					sents = getSents(orig_para, para_edits)
					# Save the sents in the coder_dict
					coder_dict[coder].extend(sents)
			# Get the sorted coder ids
			coder_ids = sorted(coder_dict.keys())
			# Loop through the sentences for the first coder
			for sent_id, sent in enumerate(coder_dict[0]):
				# Write the original sentence to the output M2 file
				out_m2.write("S "+" ".join(sent["orig"])+"\n")
				# Annotate the original sentence with spacy
				orig_sent = toolbox.applySpacy(sent["orig"], nlp)
				# Loop through the coders
				for coder in coder_ids:
					# Annotate the corrected sentence with spacy and get the gold edits
					cor_sent = toolbox.applySpacy(coder_dict[coder][sent_id]["cor"], nlp)
					gold_edits = coder_dict[coder][sent_id]["edits"]
					# Gold edits
					if args.gold:
						# Make sure edits are ordered in terms of start, then end offsets.
						gold_edits = sorted(gold_edits, key=itemgetter(0)) # Sort by start offset
						gold_edits = sorted(gold_edits, key=itemgetter(1)) # Sort by end offset
						min_edits = []
						# Loop through the gold edits.
						for gold_edit in gold_edits:
							# Minimise correction (not detection D) edits: e.g. [has eaten -> eaten] = [has -> ε]
							if gold_edit[2] == "C": gold_edit = toolbox.minimiseEdit(gold_edit, orig_sent, cor_sent)
							# Classify and save non-empty edits
							if gold_edit:
								cat = cat_rules.autoTypeEdit(gold_edit, orig_sent, cor_sent, gb_spell, tag_map, nlp, stemmer)
								gold_edit[2] = cat
								min_edits.append(gold_edit)
						# If there are no minimised edits, write an explicit empty edit
						if not min_edits:
							out_m2.write("A -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||"+str(coder)+"\n")
						# Otherwise loop through the edits and write them to the output m2 file.
						for edit in min_edits: out_m2.write(toolbox.formatEdit(edit, coder)+"\n")
					# Auto edits
					elif args.auto:
						# Auto align the parallel sentences and extract the edits.
						auto_edits = align_text.getAutoAlignedEdits(orig_sent, cor_sent, nlp, args)
						# If there are no edits, write an explicit noop edit.
						if not auto_edits:
							out_m2.write("A -1 -1|||noop|||-NONE-|||REQUIRED|||-NONE-|||"+str(coder)+"\n")
						# Loop through the edits.
						for auto_edit in auto_edits:
							# Give each edit an automatic error type.
							cat = cat_rules.autoTypeEdit(auto_edit, orig_sent, cor_sent, gb_spell, tag_map, nlp, stemmer)
							auto_edit[2] = cat
							# Write the edit to the output m2 file.
							out_m2.write(toolbox.formatEdit(auto_edit, coder)+"\n")
				# Write new line after each sentence when we reach last coder.
				out_m2.write("\n")

# Input 1: An essay string.
# Input 2: A list of character edits in the essay
# Input 3: A string normalisation dictionary for unusual punctuation etc.
# Output: A list of paragraph strings and their edits [(para, edits), ...]
def getParas(text, edits, norm_dict):
	para_info = []
	# Loop through all sequences between newlines
	for para in re.finditer("[^\n]+", text):
		para_edits = []
		# Keep track of correction spans (not detection spans)
		cor_spans = []
		# Loop through the edits: [start, end, cor, <type>]
		for edit in edits:
			# Find edits that fall inside this paragraph
			if edit[0] >= para.start(0) and edit[1] <= para.end(0):
				# Adjust offsets and add C or D type for correction or detection
				new_edit = [edit[0]-para.start(0), edit[1]-para.start(0), "C", edit[2]]
				if edit[2] == None: new_edit[2] = "D"
				# Normalise the string if its a correction edit
				if new_edit[2] == "C":
					new_edit[3] = edit[2].translate(norm_dict)
					# Save the span in cor_spans
					cor_spans.append(new_edit[:2])
				# Save the edit
				para_edits.append(new_edit)
			# Activate this switch to see the cross paragraph edits that are ignored, if any.
#			elif edit[0] >= para.start(0) and edit[0] <= para.end(0) and edit[1] > para.end(0):
#				print(text)
#				print(edit)
		# Remove overlapping detection edits from the list (for FCE only)
		new_para_edits = []
		# Loop through the new normalised edits again
		for edit in para_edits:
			# Find detection edits
			if edit[2] == "D":
				# Boolean if the edit overlaps with a correction
				overlap = False
				# Loop through cor_spans
				for start, end in cor_spans:
					# Check whether there are any correction edits inside this detection edit.
					if (start != end and start >= edit[0] and end <= edit[1]) or \
					   (start == end and start > edit[0] and end < edit[1]): overlap = True
				# If there is an overlap, ignore the detection edit
				if overlap: continue
			new_para_edits.append(edit)
		# Save the para and the para edits
		para_info.append((para.group(0), new_para_edits))
	return para_info

# Input 1: An untokenized paragraph string.
# Input 2: A list of character edits in the input string.
# Output 1: The same as Input 1, except unnecessary whitespace has been removed.
# Output 2: The same as Input 2, except character edit spans have been updated.
def cleanPara(para, edits):
	# Replace all types of whitespace with a space
	para = re.sub("\s", " ", para)
	# Find any sequence of 2 adjacent whitespace characters
	# NOTE: Matching only 2 at a time lets us preserve edits between multiple whitespace.
	match = re.search("  ", para)
	# While there is a match...
	while match:
		# Find the index where the whitespace starts.
		ws_start = match.start()
		# Remove 1 of the whitespace chars.
		para = para[:ws_start] + para[ws_start+1:]
		# Update affected edits that start after ws_start
		for edit in edits:
			# edit = [start, end, ...]
			if edit[0] > ws_start:
				edit[0] -= 1
			if edit[1] > ws_start:
				edit[1] -= 1
		# Try matching again
		match = re.search("  ", para)
	# Remove leading whitespace, if any.
	if para.startswith(" "):
		para = para.lstrip()
		# Subtract 1 from all edits.
		for edit in edits:
			# edit = [start, end, ...]
			# "max" used to prevent negative index
			edit[0] = max(edit[0] - 1, 0)
			edit[1] = max(edit[1] - 1, 0)
	# Remove whitespace leading/trailing whitespace from character edit spans
	for edit in edits:
		# Ignore insertions
		if edit[0] == edit[1]: continue
		# Get the orig text
		orig = para[edit[0]:edit[1]]
		# Remove leading whitespace and update span
		if orig.startswith(" "): edit[0] += 1
		if orig.endswith(" "): edit[1] -= 1
	# Return para and new edit spans.
	return para, edits

# Input 1: A spacy paragraph
# Input 2: A list of character edits in the input string.
# Input 3: A spacy processing object
# Output: A list of token edits that map to exact tokens.
def getTokenEdits(para, edits, nlp):
	# Get the character start and end offsets of all tokens in the para.
	tok_starts, tok_ends = getAllTokStartsAndEnds(para)
	prev_tok_end = 0
	overlap_edit_ids = []
	# edit = [start, end, cat, cor]
	for edit in edits:
		# Set cor to orig string if this is a detection edit
		if edit[3] == None: edit[3] = para.text[edit[0]:edit[1]]
		# Convert the character spans to token spans.
		span = convertCharToTok(edit[0], edit[1], tok_starts, tok_ends)
		# If chars do not map cleanly to tokens, extra processing is needed.
		if len(span) == 4:
			# Sometimes token expansion results in overlapping edits. Keep track of these.
			if span[0] < prev_tok_end:
				overlap_edit_ids.append(edits.index(edit))
				continue
			# When span len is 4, span[2] and [3] are the new char spans.
			# Use these to expand the edit to match token boundaries.
			left = para.text[span[2]:edit[0]]
			right = para.text[edit[1]:span[3]]
			# Add this new info to cor.
			edit[3] = (left+edit[3]+right).strip()
		# Keep track of prev_tok_end
		prev_tok_end = span[1]
		# Change char span to tok span
		edit[0] = span[0]
		edit[1] = span[1]
		# Tokenise correction edits
		if edit[2] == "C": edit[3] = " ".join([tok.text for tok in nlp(edit[3].strip())])
		# Set detection edits equal to the tokenised original
		elif edit[2] == "D": edit[3] = " ".join([tok.text for tok in para[edit[0]:edit[1]]])
	# Finally remove any overlap token edits from the edit list (rare)
	for id in sorted(overlap_edit_ids, reverse=True):
		del edits[id]
	return edits

# Input: A spacy paragraph
# Output: A list of character start and end positions for each token in the input.
def getAllTokStartsAndEnds(spacy_doc):
	tok_starts = []
	tok_ends = []
	for tok in spacy_doc:
		tok_starts.append(tok.idx)
		tok_ends.append(tok.idx + len(tok.text))
	return tok_starts, tok_ends

# Input 1: A char start position
# Input 2: A char end position
# Input 3: All the char token start positions in the paragraph
# Input 4: All the char token end positions in the paragraph
# Output: The char start and end position now in terms of tokens.
def convertCharToTok(start, end, all_starts, all_ends):
	# If the start and end span is the same, the edit is an insertion.
	if start == end:
		# Special case: Pre-First token edits.
		if not start or start <= all_starts[0]:
			return [0, 0]
		# Special case: Post-Last token edits.
		elif start >= all_ends[-1]:
			return [len(all_starts), len(all_starts)]
		# General case 1: Edit starts at the beginning of a token.
		elif start in all_starts:
			return [all_starts.index(start), all_starts.index(start)]
		# General case 2: Edit starts at the end of a token.
		elif start in all_ends:
			return [all_ends.index(start)+1, all_ends.index(start)+1]
		# Problem case: Edit starts inside 1 token.
		else:
			# Expand character span to nearest token boundary.
			if start not in all_starts:
				start = all_starts[bisect(all_starts, start)-1]
			if end not in all_ends:
				end = all_ends[bisect(all_ends, end)]
			# Keep the new character spans as well
			return [all_starts.index(start), all_ends.index(end)+1, start, end]
	# Character spans match complete token spans.
	elif start in all_starts and end in all_ends:
		return [all_starts.index(start), all_ends.index(end)+1]
	# Character spans do NOT match complete token spans.
	else:
		# Expand character span to nearest token boundary.
		if start not in all_starts:
			start = all_starts[bisect(all_starts, start)-1]
		if end not in all_ends:
			nearest = bisect(all_ends, end)
			# Sometimes the end is a char after the last token.
			# In this case, just use the last tok boundary.
			if nearest >= len(all_ends):
				end = all_ends[-1]
			else:
				end = all_ends[bisect(all_ends, end)]
		# Keep the new character spans as well
		return [all_starts.index(start), all_ends.index(end)+1, start, end]

# Input 1: A SpaCy original paragraph Doc object.
# Input 2: A list of edits in that paragraph.
# Output: A list of dictionaries. Each dict has 3 keys: orig, cor, edits
# Sentences are split according to orig only. Edits map orig to cor.
def getSents(orig, edits):
	sent_list = []
	# Make sure spacy sentences end in punctuation where possible.
	orig_sents = []
	start = 0
	for sent in orig.sents:
		# Only save sentence boundaries that end with punctuation or are paragraph final.
		if sent[-1].text[-1] in punctuation or sent.end == len(orig):
			orig_sents.append(orig[start:sent.end])
			start = sent.end
	# If orig is 1 sentence, just return.
	if len(orig_sents) == 1:
		# Sents are list of tokens. Edits have cor spans added.
		orig, cor, edits = prepareSentEditsOutput(orig, edits)
		out_dict = {"orig": orig,
					"cor": cor,
					"edits": edits}
		sent_list.append(out_dict)
	# Otherwise, we need to split up the paragraph.
	else:
		# Keep track of processed edits (assumes ordered edit list)
		proc = 0
		# Keep track of diff between orig and cor sent based on applied edits.
		cor_offset = 0
		# Loop through the original sentences.
		for sent_id, orig_sent in enumerate(orig_sents):
			# Store valid edits here
			sent_edits = [] 
			# Loop through unprocessed edits
			for edit in edits[proc:]:
				# edit = [orig_start, orig_end, cat, cor]
				# If edit starts inside the current sentence but ends outside it...
				if orig_sent.start <= edit[0] < orig_sent.end and edit[1] > orig_sent.end:
					# We cannot handle cross orig_sent edits, so just ignore them.
					# Update cor_offset and proc_cnt
					cor_offset = cor_offset-(edit[1]-edit[0])+len(edit[3].split())
					proc += 1
				# If the edit starts before the last token and ends inside the sentence...
				elif orig_sent.start <= edit[0] < orig_sent.end and edit[1] <= orig_sent.end:
					# It definitely belongs to this sentence, so save it.
					# Update the token spans to reflect the new boundary
					edit[0] -= orig_sent.start # Orig_start
					edit[1] -= orig_sent.start # Orig_end
					# Update cor_offset and proc_cnt
					cor_offset = cor_offset-(edit[1]-edit[0])+len(edit[3].split())
					proc += 1
					# Save the edit
					sent_edits.append(edit)
				# If the edit starts and ends after the last token..
				elif edit[0] == edit[1] == orig_sent.end:
					# It could ambiguously belong to this, or the next sentence.
					# If this is the last sentence, the cor is null, or the last char in cor 
					# is punct, then the edit belongs to the current sent.
					if sent_id == len(orig_sents)-1 or not edit[3] or edit[3][-1] in punctuation:
						# Update the token spans to reflect the new boundary
						edit[0] -= orig_sent.start # Orig_start
						edit[1] -= orig_sent.start # Orig_end
						# Update cor_offset and proc_cnt
						cor_offset = cor_offset-(edit[1]-edit[0])+len(edit[3].split())
						proc += 1
						# Save the edit
						sent_edits.append(edit)
				# In all other cases, edits likely belong to a different sentence.
			# Sents are list of tokens. Edits have cor spans added.
			orig_sent, cor_sent, sent_edits = prepareSentEditsOutput(orig_sent, sent_edits)
			# Save orig sent and edits
			out_dict = {"orig": orig_sent,
						"cor": cor_sent,
						"edits": sent_edits}
			sent_list.append(out_dict)
	return sent_list

# Input 1: A tokenized original sentence.
# Input 2: The edits in that sentence.
# Output 1: The tokenized corrected sentence from these edits.
# Output 2: The edits, now containing the tok span of cor_str in cor_sent.
def prepareSentEditsOutput(orig, edits):
	orig = [tok.text for tok in orig]
	cor = orig[:]
	offset = 0
	for edit in edits:
		# edit = [orig_start, orig_end, cat, cor]
		cor_toks = edit[3].split()
		cor[edit[0]+offset:edit[1]+offset] = cor_toks
		cor_start = edit[0]+offset
		cor_end = cor_start+len(cor_toks)
		offset = offset-(edit[1]-edit[0])+len(cor_toks)
		# Save cor offset
		edit.extend([cor_start, cor_end])
	return orig, cor, edits


if __name__ == "__main__":
	# Define and parse program input
	parser = argparse.ArgumentParser(description="Convert Cambridge English Write & Improve (WI) (and new public FCE) JSON to M2 format.",
									formatter_class=argparse.RawTextHelpFormatter,
									usage="%(prog)s [-h] (-auto | -gold) [options] json_file -out <out_name>")
	parser.add_argument("json_file", help="Path to a WI/FCE JSON file, one JSON essay per line.")
	parser.add_argument("-out", help="Output M2 filename.", required=True)
	type_group = parser.add_mutually_exclusive_group(required=True)
	type_group.add_argument("-auto", help="Extract edits automatically.", action="store_true")
	type_group.add_argument("-gold", help="Preserve human edit spans.", action="store_true")
	parser.add_argument("-lev", help="Extract edits using basic Levenshtein.", action="store_true")
	parser.add_argument("-merge", help="Choose a merging strategy for an automatic alignment.\n"
						"all-split: Merge nothing; e.g. MSSDI -> M, S, S, D, I\n"
						"all-merge: Merge adjacent non-matches; e.g. MSSDI -> M, SSDI\n"
						"all-equal: Merge adjacent same-type non-matches; e.g. MSSDI -> M, SS, D, I\n"
						"rules: Use ERRANTs rule-based merging strategy (default)",
						default="rules")
	args = parser.parse_args()
	main(args)
